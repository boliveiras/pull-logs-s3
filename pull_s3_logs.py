#!/usr/bin/env python3
"""Faz o pull de logs de um bucket S3 para um diretório local.

Para cada objeto do bucket:
  - se estiver compactado (.gz), baixa e descompacta para .log;
  - se já estiver em texto (.log), apenas baixa.

Os arquivos ficam numa pasta de staging que um agente (ex.: NXLog) observa
(tail) para encaminhar ao SIEM.

Fluxo:  S3  ->  este script (pull)  ->  armazenamento local  ->  agente  ->  SIEM

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

logger = logging.getLogger("pull_s3_logs")

# Sinalizador de parada para encerrar o loop com elegância (Ctrl+C / SIGTERM).
_stop = False


@dataclass
class Config:
    """Configuração lida do ambiente."""

    bucket_name: str
    download_dir: Path        # staging de download dos arquivos compactados
    log_dir: Path             # onde os .log ficam (pasta observada pelo agente)
    state_file: Path          # guarda o timestamp da última sincronização
    exec_log_file: Path       # log de execução deste script
    prefix_template: str      # prefixo do S3, ex.: "%Y%m%d/" (data UTC)
    interval_seconds: int     # intervalo entre execuções no modo loop
    aws_region: str | None

    @classmethod
    def from_env(cls) -> "Config":
        base = Path(os.environ.get("PULL_BASE_DIR", "D:/Logs"))
        return cls(
            bucket_name=os.environ.get("S3_BUCKET", "your-s3-logs-bucket"),
            download_dir=base,
            log_dir=Path(os.environ.get("PULL_LOG_DIR", base / "Logs")),
            state_file=Path(os.environ.get("PULL_STATE_FILE", base / "ultima_sincronizacao.txt")),
            exec_log_file=Path(os.environ.get("PULL_EXEC_LOG", "C:/LogFiles/pull-logs-s3/sync_execution.log")),
            prefix_template=os.environ.get("S3_PREFIX_TEMPLATE", "%Y%m%d/"),
            interval_seconds=int(os.environ.get("PULL_INTERVAL_SECONDS", "60")),
            aws_region=os.environ.get("AWS_REGION") or None,
        )


def load_env_file(path: Path) -> None:
    """Carrega um arquivo .env simples (KEY=VALUE) para o ambiente.

    Não sobrescreve variáveis já definidas — as do sistema/serviço têm
    prioridade. Linhas em branco e comentários (#) são ignorados. Mantém o
    script sem dependências extras (não precisa de python-dotenv).
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def warn_if_world_readable(path: Path) -> None:
    """Em sistemas POSIX, alerta se o .env estiver acessível por grupo/outros.

    O .env pode guardar segredos (chaves AWS), então deve ter permissão
    restrita (ex.: chmod 640). No Windows o controle é via ACL (veja o README).
    """
    if os.name != "posix" or not path.exists():
        return
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        logger.warning(
            "Permissões frouxas em %s (%o). Restrinja com 'chmod 640' — "
            "o arquivo pode conter segredos.", path, mode,
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


def is_compressed(key: str) -> bool:
    """Indica se o objeto do S3 está compactado em gzip (.gz)."""
    return key.lower().endswith(".gz")


def staging_log_name(filename: str) -> str:
    """Nome do arquivo final na pasta de staging (sempre termina em .log).

    Remove o sufixo .gz e garante a extensão .log para o agente conseguir
    captar via glob '*.log'. Ex.: 'evt.log.gz' -> 'evt.log';
    'evt.json.gz' -> 'evt.json.log'; 'evt.gz' -> 'evt.log'.
    """
    name = filename
    if name.lower().endswith(".gz"):
        name = name[:-3]
    if not name.lower().endswith(".log"):
        name += ".log"
    return name


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
            compressed = is_compressed(key)
            dest = cfg.log_dir / (staging_log_name(filename) if compressed else filename)

            # Evita rebaixar/reprocessar o que já existe localmente.
            if dest.exists():
                logger.debug("Já processado, ignorando: %s", filename)
            elif compressed:
                # Compactado: baixa o .gz para o staging e descompacta para .log.
                tmp_gz = cfg.download_dir / filename
                s3.download_file(cfg.bucket_name, key, str(tmp_gz))
                logger.info("Baixado (compactado): %s", key)
                decompress(tmp_gz, dest)
                downloaded += 1
            else:
                # Já em texto: baixa direto para a pasta observada pelo agente.
                s3.download_file(cfg.bucket_name, key, str(dest))
                logger.info("Baixado (texto): %s", key)
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
    parser = argparse.ArgumentParser(description="Faz o pull de logs de um bucket S3 para o disco local.")
    parser.add_argument("--once", action="store_true", help="Executa um único ciclo e sai.")
    parser.add_argument(
        "--env-file",
        default=os.environ.get("PULL_ENV_FILE", str(Path(__file__).with_name(".env"))),
        help="Caminho do .env (padrão: .env na mesma pasta do script).",
    )
    args = parser.parse_args()

    # Carrega o .env (se existir) antes de ler a configuração.
    env_path = Path(args.env_file)
    load_env_file(env_path)
    cfg = Config.from_env()
    setup_logging(cfg.exec_log_file)
    warn_if_world_readable(env_path)

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
