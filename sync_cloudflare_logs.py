#!/usr/bin/env python3
"""Sincroniza logs da Cloudflare (Logpush -> S3) para um diretório local.

Os arquivos .gz são baixados do bucket S3, descompactados para .log e deixados
em uma pasta que o NXLog observa (tail) para encaminhar ao SIEM.

Fluxo:  S3  ->  este script (pull)  ->  armazenamento local  ->  NXLog  ->  SIEM

A configuração vem de variáveis de ambiente (veja .env.example). Nada de
credencial ou caminho fica embutido no código.
"""

from __future__ import annotations

import argparse
import gzip
import logging
import os
import shutil
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger("cloudflare_sync")

# Sinalizador de parada para encerrar o loop com elegância (Ctrl+C / SIGTERM).
_stop = False


@dataclass
class Config:
    """Configuração lida do ambiente."""

    bucket_name: str
    download_dir: Path        # onde os .gz são baixados
    log_dir: Path             # onde os .log descompactados ficam (lidos pelo NXLog)
    state_file: Path          # guarda o timestamp da última sincronização
    exec_log_file: Path       # log de execução deste script
    prefix_template: str      # prefixo do S3, ex.: "%Y%m%d/" (data UTC)
    interval_seconds: int     # intervalo entre execuções no modo loop
    aws_region: str | None

    @classmethod
    def from_env(cls) -> "Config":
        base = Path(os.environ.get("CF_BASE_DIR", "D:/Cloudflare"))
        return cls(
            bucket_name=os.environ.get("CF_BUCKET", "your-cloudflare-logs-bucket"),
            download_dir=base,
            log_dir=Path(os.environ.get("CF_LOG_DIR", base / "Logs")),
            state_file=Path(os.environ.get("CF_STATE_FILE", base / "ultima_sincronizacao.txt")),
            exec_log_file=Path(os.environ.get("CF_EXEC_LOG", "C:/LogFiles/Cloudflare/sync_execution.log")),
            prefix_template=os.environ.get("CF_PREFIX_TEMPLATE", "%Y%m%d/"),
            interval_seconds=int(os.environ.get("CF_INTERVAL_SECONDS", "60")),
            aws_region=os.environ.get("AWS_REGION") or None,
        )


def setup_logging(exec_log_file: Path) -> None:
    """Configura logging para console + arquivo com rotação (5 MB x 5)."""
    exec_log_file.parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    # No Windows o console pode estar em cp1252; força UTF-8 para não truncar acentos.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    file_handler = RotatingFileHandler(
        exec_log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)

    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


def _parse_dt(value: str) -> datetime:
    """Converte ISO-8601 para datetime com timezone (assume UTC se vier sem)."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_watermark(state_file: Path) -> datetime | None:
    """Lê o timestamp da última sincronização, se existir."""
    if not state_file.exists():
        return None
    try:
        return _parse_dt(state_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError) as exc:
        logger.warning("Não foi possível ler o estado de sincronização: %s", exc)
        return None


def save_watermark(state_file: Path, value: datetime) -> None:
    """Persiste o timestamp da última sincronização."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(value.isoformat(), encoding="utf-8")


def decompress(gz_path: Path, dest_path: Path) -> None:
    """Descompacta um .gz para .log usando streaming (baixo uso de memória)."""
    with gzip.open(gz_path, "rb") as f_in, open(dest_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    logger.info("Arquivo descompactado: %s", dest_path)


def iter_new_objects(s3, cfg: Config, watermark: datetime | None):
    """Itera os objetos do prefixo do dia, com paginação, mais novos que o watermark."""
    prefix = datetime.now(timezone.utc).strftime(cfg.prefix_template)
    paginator = s3.get_paginator("list_objects_v2")

    found_any = False
    for page in paginator.paginate(Bucket=cfg.bucket_name, Prefix=prefix):
        for obj in page.get("Contents", []):
            found_any = True
            if watermark and obj["LastModified"] <= watermark:
                continue
            yield obj

    if not found_any:
        logger.info("Nenhum log encontrado no bucket para o prefixo '%s'.", prefix)


def sync_once(s3, cfg: Config) -> None:
    """Executa um ciclo de sincronização."""
    watermark = load_watermark(cfg.state_file)
    newest = watermark
    downloaded = 0

    try:
        for obj in iter_new_objects(s3, cfg, watermark):
            key = obj["Key"]
            filename = os.path.basename(key)
            gz_path = cfg.download_dir / filename
            log_path = cfg.log_dir / filename.replace(".gz", ".log")

            # Evita rebaixar/reprocessar o que já existe localmente.
            if log_path.exists():
                logger.debug("Já processado, ignorando: %s", filename)
            else:
                s3.download_file(cfg.bucket_name, key, str(gz_path))
                logger.info("Baixado: %s", key)
                decompress(gz_path, log_path)
                downloaded += 1

            if newest is None or obj["LastModified"] > newest:
                newest = obj["LastModified"]

        if downloaded and newest is not None:
            save_watermark(cfg.state_file, newest)
            logger.info("Última sincronização atualizada para: %s", newest.isoformat())
        elif downloaded == 0:
            logger.info("Nada novo para baixar.")

    except (ClientError, BotoCoreError) as exc:
        logger.error("Erro de S3 na sincronização: %s", exc)
    except OSError as exc:
        logger.error("Erro de I/O na sincronização: %s", exc)


def _handle_stop(signum, _frame) -> None:
    global _stop
    _stop = True
    logger.info("Sinal %s recebido. Encerrando após o ciclo atual...", signum)


def run_loop(s3, cfg: Config) -> None:
    """Roda em loop até receber Ctrl+C / SIGTERM."""
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    while not _stop:
        sync_once(s3, cfg)
        logger.info("Ciclo concluído. Aguardando %ss.", cfg.interval_seconds)
        # Espera fracionada para responder rápido ao sinal de parada.
        for _ in range(cfg.interval_seconds):
            if _stop:
                break
            time.sleep(1)

    logger.info("Sincronização encerrada.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sincroniza logs da Cloudflare do S3 para o disco local.")
    parser.add_argument("--once", action="store_true", help="Executa um único ciclo e sai.")
    args = parser.parse_args()

    cfg = Config.from_env()
    setup_logging(cfg.exec_log_file)

    cfg.download_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)

    session = boto3.session.Session(region_name=cfg.aws_region)
    s3 = session.client("s3")

    logger.info("Iniciando sincronização do bucket '%s'.", cfg.bucket_name)

    if args.once:
        sync_once(s3, cfg)
    else:
        run_loop(s3, cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
