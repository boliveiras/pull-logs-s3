# Cloudflare Logs: do S3 para o SIEM

Pequeno pipeline para trazer os logs da Cloudflare (entregues via **Logpush**
num bucket S3) até o SIEM, passando por um servidor Windows que faz o
encaminhamento com o **NXLog**.

## A ideia

A Cloudflare não envia os logs direto pro nosso SIEM. Ela deposita arquivos
`.gz` num bucket S3. Então o caminho é:

```
  Cloudflare Logpush
         │
         ▼
   ┌───────────┐     ┌──────────────────────┐     ┌──────────────────┐     ┌────────┐
   │  Bucket   │ --> │  sync_cloudflare_    │ --> │  Disco local      │ --> │ NXLog  │ --> SIEM
   │   S3      │     │  logs.py (pull)      │     │  D:\Cloudflare    │     │ (tail) │
   └───────────┘     └──────────────────────┘     └──────────────────┘     └────────┘
```

1. **S3** — a Cloudflare grava os logs compactados (`.gz`), organizados por data.
2. **Script Python** — baixa os arquivos novos, descompacta para `.log` e
   guarda numa pasta local. Roda em loop (ou uma vez só).
3. **Disco local** — área de "staging". O NXLog observa essa pasta.
4. **NXLog** — faz o *tail* dos `.log` e manda pro SIEM via UDP. Esse mesmo
   NXLog ainda coleta os Event Logs do Windows (essa parte já vinha junto).
5. **SIEM** — recebe tudo e indexa.

A limpeza dos arquivos já processados fica por conta do próprio NXLog (tem um
agendamento que apaga `.gz` e `.log` antigos), então o disco não enche.

## Arquivos

| Arquivo | Para que serve |
|---|---|
| `sync_cloudflare_logs.py` | Baixa e descompacta os logs do S3. |
| `nxlog.conf` | Config do NXLog (Community Edition) que lê os logs e envia ao SIEM. |
| `.env.example` | Modelo das variáveis de ambiente. Copie para `.env` e ajuste. |
| `requirements.txt` | Dependências Python (só o `boto3`). |

## Pré-requisitos (importante)

Para a coleta funcionar, a máquina precisa de:

1. **Python 3.9+** e o `boto3` (veja `requirements.txt`).
2. **AWS CLI instalado** na máquina, com um par **Access Key / Secret Key**
   configurado. Essas credenciais precisam ter **permissão de leitura no
   bucket S3** (`s3:ListBucket` e `s3:GetObject`). Sem isso o script não
   consegue listar nem baixar os logs.

A forma mais simples de configurar as credenciais é via AWS CLI:

```powershell
aws configure
# AWS Access Key ID:     <sua access key>
# AWS Secret Access Key: <sua secret key>
# Default region name:   us-east-1
```

Isso grava as credenciais em `%USERPROFILE%\.aws\credentials`, e o `boto3`
as lê automaticamente — não é preciso colocar chave nenhuma no código.

> Recomendação de segurança: dê à chave **somente leitura** no bucket de logs.
> Em ambientes AWS (EC2, etc.) o ideal é usar uma **IAM Role** no lugar da
> chave estática.

## Como rodar o script

```powershell
# 1. Instalar dependências
pip install -r requirements.txt

# 2. Configurar credenciais da AWS (uma vez)
aws configure

# 3. Configurar o script (copie e edite com seus valores)
copy .env.example .env

# 4. Carregar as variáveis e rodar
#    (em produção, isso normalmente vai num serviço / Agendador de Tarefas)
python sync_cloudflare_logs.py          # loop contínuo
python sync_cloudflare_logs.py --once   # só um ciclo, útil pra testar
```

A configuração do script (bucket, caminhos, intervalo) vem toda de variáveis de
ambiente — nada fica embutido no código.

## NXLog

O `nxlog.conf` é compatível com a **Community Edition**. Antes de usar, ajuste
os `define` no topo do arquivo:

- `SIEM_HOST`, `SIEM_PORT_WINEVT`, `SIEM_PORT_CF` — destino do SIEM.
- `CF_LOGDIR` — a mesma pasta onde o script Python solta os `.log`.

Coloque o arquivo em `C:\Program Files\nxlog\conf\nxlog.conf` (ou ajuste o
caminho da sua instalação) e reinicie o serviço do NXLog.

## O que mudou em relação à versão original

A lógica é a mesma; a refatoração deixou o script mais robusto e fácil de manter:

- **Configuração externa** — saiu tudo do código pra variáveis de ambiente.
- **Logging com rotação** — usa o módulo `logging` com `RotatingFileHandler`,
  em vez de abrir o arquivo a cada mensagem.
- **Paginação no S3** — agora lê além de 1000 objetos por listagem.
- **Descompactação em streaming** — não carrega o arquivo inteiro na memória.
- **Datas com timezone** — o `LastModified` do S3 é UTC; o controle da última
  sincronização passou a respeitar isso e evita comparações quebradas.
- **Não reprocessa** o que já existe localmente.
- **Parada limpa** — responde a `Ctrl+C` / `SIGTERM` sem cortar no meio.
- **Modo `--once`** pra testar sem entrar no loop.

## Observações de segurança

- Todos os IPs, portas, nome de bucket e caminhos nos arquivos são **exemplos
  genéricos**. Troque pelos valores reais só no seu `.env` / na sua instalação.
- `.env`, arquivos de log e os próprios logs baixados estão no `.gitignore`.

## Licença

Distribuído sob a licença **MIT**. Veja o arquivo [`LICENSE`](LICENSE).
