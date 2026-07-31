# Mautic Safe Upgrade

Assistente seguro para diagnosticar, preparar, fazer backup, atualizar, validar e reverter migrações de **Mautic 3/4 para Mautic 5 LTS**.

O objetivo não é apenas rodar comandos de atualização. O foco é reduzir risco de perda de dados, erro 500, quebra de campanhas, perda de contatos e indisponibilidade prolongada.

## Status

Protótipo funcional em Python.

Ele já automatiza diagnóstico, backup local, manifesto com SHA-256, validação do backup, geração de plano, execução controlada de comandos seguros do console Mautic e rollback de arquivos.

Algumas etapas continuam semiassistidas por segurança, como troca de PHP, troca de imagem Docker, troca de pacote/composer, pausa de crons e restauração do banco.

## Princípios de Segurança

- Nunca executar upgrade sem backup validado.
- Nunca mascarar erro crítico como sucesso.
- Nunca apagar dados sem confirmação explícita.
- Toda etapa precisa ser auditável.
- O cliente precisa entender claramente o que está acontecendo.
- Antes do backup local, o operador precisa confirmar que fez snapshot/backup completo do servidor.

## Requisitos

- Python 3.10 ou superior.
- Acesso SSH/terminal ao servidor onde o Mautic roda.
- Acesso de leitura e escrita ao diretório do Mautic.
- `php` disponível no terminal.
- `mysqldump` disponível quando o backup do banco for automático.
- Espaço em disco suficiente para backup dos arquivos e dump do banco.

## Instalação

Clone ou copie este diretório para o servidor:

```bash
cd /opt
git clone https://github.com/SEU-USUARIO/mautic-safe-upgrade.git
cd mautic-safe-upgrade
```

Você pode executar direto:

```bash
python3 msu.py --help
```

Ou instalar o comando `msu` no servidor:

```bash
sudo ./install.sh
msu --help
```

## Fluxo Rápido

### 1. Diagnosticar

```bash
python3 msu.py --path /var/www/mautic diagnose
```

O diagnóstico identifica versão do Mautic, PHP, extensões, disco, plugins, temas, sinais de Docker/Plesk/cPanel e riscos.

### 2. Fazer snapshot/backup do servidor

Antes de continuar, faça um backup completo fora do Mautic:

- snapshot da VPS;
- snapshot do volume;
- backup no painel do provedor;
- backup via Portainer;
- backup completo no Plesk/cPanel;
- outra rotina confiável do seu ambiente.

Depois confirme explicitamente:

```bash
python3 msu.py --path /var/www/mautic backup --storage /backups --confirm "CONFIRMO QUE FIZ BACKUP DO SERVIDOR"
```

Sem essa frase, o backup local não roda.

### 3. Gerar plano de upgrade

```bash
python3 msu.py --path /var/www/mautic plan --target 5.2-lts
```

O plano mostra as etapas necessárias e bloqueios encontrados.

### 4. Simular upgrade

```bash
python3 msu.py --path /var/www/mautic upgrade --confirm "CONFIRMO UPGRADE"
```

Sem `--execute`, o comando não altera o Mautic. Ele mostra os comandos seguros previstos.

### 5. Executar etapas automáticas seguras

```bash
python3 msu.py --path /var/www/mautic upgrade --confirm "CONFIRMO UPGRADE" --execute
```

Esse comando só roda depois de encontrar backup validado.

### 6. Validar depois do upgrade

```bash
python3 msu.py --path /var/www/mautic validate
```

### 7. Rollback de arquivos

```bash
python3 msu.py --path /var/www/mautic rollback --confirm "CONFIRMO ROLLBACK"
```

Para restaurar de fato os arquivos:

```bash
python3 msu.py --path /var/www/mautic rollback --confirm "CONFIRMO ROLLBACK" --execute
```

Importante: a restauração do banco ainda deve ser feita de forma controlada usando o `database.sql` do backup validado.

## Estrutura dos Backups

Os backups são salvos no diretório informado em `--storage`:

```text
/backups/
  mautic-safe-upgrade-20260731T120000Z-a1b2c3d4/
    mautic-files.tar.gz
    database.sql
    manifest.json
```

O `manifest.json` contém tamanho, SHA-256 e status de validação.

## O que o Protótipo Automatiza

- Diagnóstico do ambiente.
- Detecção básica de credenciais em `local.php` ou `.env`.
- Mensagem obrigatória para backup/snapshot do servidor.
- Backup compactado dos arquivos.
- Dump do banco com `mysqldump`, quando possível.
- Manifesto com SHA-256.
- Validação de integridade do backup.
- Plano de migração para Mautic 5.2 LTS.
- Execução controlada de comandos seguros:
  - `cache:clear`;
  - `mautic:update:apply --finish`;
  - `doctrine:migration:migrate --no-interaction`.
- Validação pós-upgrade via console.
- Rollback de arquivos a partir de backup validado.

## O que Ainda é Semiassistido

- Ajuste da versão do PHP.
- Troca de imagem Docker.
- Troca de pacote oficial Mautic.
- Atualização via Composer.
- Pausa e reativação de crons.
- Desativação de plugins incompatíveis.
- Restauração do banco em rollback.
- Liberação do tráfego de produção.

Essas etapas variam muito entre Docker Compose, Docker Swarm, VPS manual, Plesk, cPanel, Traefik, Cloudflare e instalações customizadas.

## Documentação

- [Guia de uso no GitHub](docs/uso-github.md)
- [Automação operacional](docs/automacao-operacional.md)
- [Documento de arquitetura](docs/architecture.md)
- [Fluxograma da migração](docs/migration-flow.md)
- [Checklist pré-upgrade](docs/pre-upgrade-checklist.md)
- [Checklist pós-upgrade](docs/post-upgrade-checklist.md)
- [Riscos e mitigação](docs/risks.md)
- [Plano de testes](docs/test-plan.md)
- [Roadmap](docs/roadmap.md)
- [Estrutura inicial e pseudocódigo](spec/initial-structure-and-pseudocode.md)

## Aviso Importante

Use primeiro em ambiente de teste ou homologação. Em produção, combine este assistente com snapshot do servidor, janela de manutenção, validação humana e plano de rollback.
