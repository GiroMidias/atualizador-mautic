#!/usr/bin/env python3
"""
Mautic Safe Upgrade

Diagnostica instalações Mautic diretas ou Docker, cria backup local validado,
gera plano de upgrade, executa comandos seguros pós-upgrade e permite rollback.

Este script não instala pacotes do sistema e não altera Docker/Compose/Swarm sozinho.
A troca de imagem/container fica no msu-install.sh, com confirmação explícita.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
import uuid
from pathlib import Path
from typing import Any

SERVER_BACKUP_PHRASE = "CONFIRMO QUE FIZ BACKUP DO SERVIDOR"
UPGRADE_PHRASE = "CONFIRMO UPGRADE"
ROLLBACK_PHRASE = "CONFIRMO ROLLBACK"


def now_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def env_get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def sanitize(text: str) -> str:
    patterns = [
        (r"(?i)(password|passwd|pwd|secret|token|api[_-]?key)(\s*[:=]\s*)[^\s'\"]+", r"\1\2[REDACTED]"),
        (r"(?i)(Authorization:\s*Bearer\s+)[^\s]+", r"\1[REDACTED]"),
        (r"mysql://([^:]+):([^@]+)@", r"mysql://\1:[REDACTED]@"),
    ]
    for pattern, repl in patterns:
        text = re.sub(pattern, repl, text)
    return text


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 60, env: dict[str, str] | None = None) -> dict[str, Any]:
    try:
        p = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env=env,
        )
        return {
            "ok": p.returncode == 0,
            "code": p.returncode,
            "stdout": sanitize(p.stdout.strip()),
            "stderr": sanitize(p.stderr.strip()),
            "cmd": " ".join(cmd),
        }
    except FileNotFoundError:
        return {"ok": False, "code": 127, "stdout": "", "stderr": f"Comando não encontrado: {cmd[0]}", "cmd": " ".join(cmd)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": 124, "stdout": "", "stderr": "Tempo esgotado", "cmd": " ".join(cmd)}


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def state_dir(mautic_path: Path) -> Path:
    return mautic_path / ".msu"


def docker_context_from_env() -> dict[str, str]:
    keys = {
        "install_type": "MSU_INSTALL_TYPE",
        "detected_version": "MSU_DETECTED_MAUTIC_VERSION",
        "container_id": "MSU_DOCKER_CONTAINER_ID",
        "container_name": "MSU_DOCKER_CONTAINER_NAME",
        "image": "MSU_DOCKER_IMAGE",
        "target_image": "MSU_TARGET_IMAGE",
        "host_path": "MSU_DOCKER_HOST_PATH",
        "compose_project": "MSU_DOCKER_COMPOSE_PROJECT",
        "compose_service": "MSU_DOCKER_COMPOSE_SERVICE",
        "compose_files": "MSU_DOCKER_COMPOSE_FILES",
        "compose_workdir": "MSU_DOCKER_COMPOSE_WORKDIR",
        "swarm_service": "MSU_DOCKER_SWARM_SERVICE",
    }
    return {key: env_get(env) for key, env in keys.items() if env_get(env)}


def write_docker_context(path: Path) -> None:
    ctx = docker_context_from_env()
    if ctx:
        write_json(state_dir(path) / "docker-context.json", ctx)


def version_major(version: str | None) -> int | None:
    if not version:
        return None
    m = re.match(r"(\d+)", version)
    return int(m.group(1)) if m else None


def detect_mautic_version(path: Path) -> str | None:
    forced = env_get("MSU_DETECTED_MAUTIC_VERSION")
    if forced:
        return forced

    console = path / "bin" / "console"
    if console.exists():
        for cmd in [["php", str(console), "mautic:version"], ["php", str(console), "--version"]]:
            result = run(cmd, cwd=path, timeout=30)
            text = f"{result.get('stdout', '')}\n{result.get('stderr', '')}"
            m = re.search(r"(\d+\.\d+(?:\.\d+)?)", text)
            if m:
                return m.group(1)

    candidates = [
        path / "app" / "version.txt",
        path / "VERSION.txt",
        path / "version.txt",
        path / "app" / "bundles" / "CoreBundle" / "Version.php",
        path / "app" / "bundles" / "CoreBundle" / "ReleaseMetadata.php",
        path / "composer.json",
        path / "composer.lock",
    ]
    for candidate in candidates:
        if candidate.exists():
            text = candidate.read_text(errors="ignore", encoding="utf-8")
            m = re.search(r"(\d+\.\d+(?:\.\d+)?)", text)
            if m:
                return m.group(1)

    image = env_get("MSU_DOCKER_IMAGE")
    if image:
        m = re.search(r"mautic[^:]*:v?(\d+(?:\.\d+)?(?:\.\d+)?)", image, flags=re.I)
        if m:
            version = m.group(1)
            return version if "." in version else f"{version}.x"
    return None


def detect_php() -> dict[str, Any]:
    result = run(["php", "-v"])
    version = None
    m = re.search(r"PHP\s+(\d+\.\d+\.\d+)", result.get("stdout", ""))
    if m:
        version = m.group(1)
    modules_result = run(["php", "-m"])
    modules = sorted({x.strip() for x in modules_result.get("stdout", "").splitlines() if x.strip() and not x.startswith("[")})
    return {"version": version, "raw": result, "modules": modules}


def list_names(path: Path) -> list[str]:
    if not path.exists() or not path.is_dir():
        return []
    return sorted([p.name for p in path.iterdir() if p.is_dir()])


def list_plugins(path: Path) -> list[str]:
    return list_names(path / "plugins") + list_names(path / "app" / "bundles")


def detect_environment(path: Path) -> dict[str, Any]:
    files = {p.name for p in path.iterdir()} if path.exists() else set()
    parent_files = {p.name for p in path.parent.iterdir()} if path.parent.exists() else set()
    compose_names = {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}
    docker = run(["docker", "ps", "--format", "{{.Names}}"], timeout=10)
    env = docker_context_from_env()
    return {
        "path": str(path),
        "exists": path.exists(),
        "has_bin_console": (path / "bin" / "console").exists(),
        "install_type": env.get("install_type", "direct"),
        "docker_available": docker["ok"],
        "docker_context": env,
        "docker_compose_files": bool(compose_names & (files | parent_files)),
        "compose_candidates": sorted(list(compose_names & (files | parent_files))),
        "plesk_hint": Path("/usr/local/psa").exists(),
        "cpanel_hint": Path("/usr/local/cpanel").exists(),
    }


def parse_local_config(path: Path) -> dict[str, str]:
    config_files = [path / "app" / "config" / "local.php", path / "config" / "local.php", path / ".env"]
    found: dict[str, str] = {}
    for cfg in config_files:
        if not cfg.exists():
            continue
        text = cfg.read_text(errors="ignore", encoding="utf-8")
        for key in ["db_host", "db_port", "db_name", "db_user", "db_password"]:
            m = re.search(rf"['\"]{key}['\"]\s*=>\s*['\"]([^'\"]*)['\"]", text)
            if m:
                found[key] = m.group(1)
        for key, env_key in {
            "db_host": "MAUTIC_DB_HOST",
            "db_port": "MAUTIC_DB_PORT",
            "db_name": "MAUTIC_DB_NAME",
            "db_user": "MAUTIC_DB_USER",
            "db_password": "MAUTIC_DB_PASSWORD",
        }.items():
            m = re.search(rf"^{env_key}\s*=\s*(.+)$", text, re.MULTILINE)
            if m:
                found[key] = m.group(1).strip().strip("'\"")
    return found


def classify_risks(diag: dict[str, Any]) -> list[dict[str, str]]:
    risks: list[dict[str, str]] = []
    env = diag["environment"]
    php_version = diag["php"].get("version")
    mautic_version = diag.get("mautic_version")
    modules = set(diag["php"].get("modules", []))

    def add(level: str, item: str, message: str) -> None:
        risks.append({"level": level, "item": item, "message": message})

    if not env["exists"]:
        add("BLOQUEADOR", "Caminho Mautic", "O caminho informado não existe.")
    if not env["has_bin_console"]:
        add("ALTO", "Console Mautic", "Não encontrei bin/console; upgrade automático completo pode não funcionar.")
    if not mautic_version:
        add("ALTO", "Versão Mautic", "Não consegui detectar a versão atual do Mautic.")
    if not php_version:
        add("BLOQUEADOR", "PHP CLI", "PHP CLI não foi encontrado ou não respondeu.")

    required_modules = {"xml", "curl", "gd", "mbstring", "intl", "zip", "bcmath"}
    missing = sorted(required_modules - modules)
    if missing:
        add("ALTO", "Extensões PHP", "Extensões ausentes: " + ", ".join(missing))

    if diag["disk"].get("free_gb", 0) < 5:
        add("BLOQUEADOR", "Disco", "Menos de 5 GB livres; backup e upgrade podem falhar.")

    current_major = version_major(mautic_version)
    target_major = version_major(env_get("MSU_TARGET_IMAGE") or env_get("MSU_TARGET") or "5")
    if current_major and target_major and current_major <= 3 and target_major >= 5:
        add("BLOQUEADOR", "Caminho de upgrade", "Mautic 3 deve ir para 4.4 antes de ir para 5+.")

    db = diag.get("database", {})
    if not db.get("db_name") or not db.get("db_user"):
        add("ALTO", "Banco", "Credenciais do banco não foram detectadas automaticamente; backup SQL automático pode falhar.")

    return risks


def print_summary(diag: dict[str, Any]) -> None:
    print(f"Mautic: {diag.get('mautic_version') or 'não detectado'}")
    print(f"PHP CLI: {diag['php'].get('version') or 'não detectado'}")
    print(f"Disco livre: {diag['disk']['free_gb']} GB")
    for risk in diag.get("risks", []):
        print(f"[{risk['level']}] {risk['item']}: {risk['message']}")


def diagnose(args: argparse.Namespace) -> None:
    path = Path(args.path).resolve()
    usage_path = path if path.exists() else path.parent
    usage = shutil.disk_usage(usage_path)
    db_safe = {k: ("[REDACTED]" if "password" in k else v) for k, v in parse_local_config(path).items()}
    diag = {
        "generated_at": now_stamp(),
        "mautic_path": str(path),
        "mautic_version": detect_mautic_version(path),
        "php": detect_php(),
        "environment": detect_environment(path),
        "docker_context": docker_context_from_env(),
        "database": db_safe,
        "disk": {"total_gb": round(usage.total / 1024**3, 2), "free_gb": round(usage.free / 1024**3, 2)},
        "plugins": list_plugins(path),
        "themes": list_names(path / "themes") + list_names(path / "app" / "bundles"),
        "cron_hint": "Confirme crons/workers externos antes de executar upgrade real.",
    }
    diag["risks"] = classify_risks(diag)
    out = Path(args.output or state_dir(path) / "diagnostic.json")
    write_json(out, diag)
    write_docker_context(path)
    print(f"Diagnóstico gerado: {out}")
    print_summary(diag)


def confirm_or_fail(phrase: str, provided: str | None, message: str) -> None:
    if provided == phrase:
        return
    print(message)
    print(f"Para continuar, rode novamente com: --confirm \"{phrase}\"")
    raise SystemExit(2)


def file_entry(path: Path) -> dict[str, Any]:
    return {"path": path.name, "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def dump_database(db: dict[str, str], out: Path) -> dict[str, Any]:
    if not db.get("db_name") or not db.get("db_user"):
        return {"ok": False, "message": "Credenciais insuficientes para dump automático."}
    cmd = ["mysqldump", "--single-transaction", "--routines", "--triggers"]
    if db.get("db_host"):
        cmd += ["-h", db["db_host"]]
    if db.get("db_port"):
        cmd += ["-P", db["db_port"]]
    cmd += ["-u", db["db_user"], db["db_name"]]
    env = os.environ.copy()
    if db.get("db_password"):
        env["MYSQL_PWD"] = db["db_password"]
    try:
        with out.open("w", encoding="utf-8") as f:
            p = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, text=True, env=env, timeout=1800, check=False)
        return {"ok": p.returncode == 0, "message": sanitize(p.stderr.strip())}
    except FileNotFoundError:
        return {"ok": False, "message": "mysqldump não encontrado."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "mysqldump excedeu o tempo limite."}


def validate_manifest(manifest: dict[str, Any], backup_dir: Path) -> bool:
    files_ok = True
    for entry in manifest.get("files", []):
        p = backup_dir / entry["path"]
        files_ok = files_ok and p.exists() and p.stat().st_size == entry["bytes"] and sha256_file(p) == entry["sha256"]
    db = manifest.get("database", {})
    db_ok = bool(db.get("skipped")) or bool(db.get("ok"))
    return files_ok and db_ok


def create_backup(args: argparse.Namespace) -> None:
    confirm_or_fail(SERVER_BACKUP_PHRASE, args.confirm, "Antes do backup local, faça snapshot/backup completo do servidor/volume no provedor.")
    path = Path(args.path).resolve()
    storage = Path(args.storage).resolve()
    backup_dir = storage / f"mautic-safe-upgrade-{now_stamp()}-{uuid.uuid4().hex[:8]}"
    backup_dir.mkdir(parents=True, mode=0o700, exist_ok=False)

    diag_path = state_dir(path) / "diagnostic.json"
    if not diag_path.exists():
        diagnose(argparse.Namespace(path=str(path), output=str(diag_path)))

    manifest: dict[str, Any] = {
        "created_at": now_stamp(),
        "mautic_path": str(path),
        "install_type": env_get("MSU_INSTALL_TYPE", "direct"),
        "docker_context": docker_context_from_env(),
        "server_backup_confirmed": True,
        "files": [],
        "valid": False,
    }

    file_archive = backup_dir / "mautic-files.tar.gz"
    with tarfile.open(file_archive, "w:gz") as tar:
        def safe_filter(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
            if "/.msu/" in ti.name or ti.name.endswith("/.msu"):
                return None
            return ti
        tar.add(path, arcname=path.name, filter=safe_filter)
    manifest["files"].append(file_entry(file_archive))

    db_config = parse_local_config(path)
    if args.skip_db:
        manifest["database"] = {"skipped": True, "reason": "Usuário pediu --skip-db"}
    else:
        db_dump = backup_dir / "database.sql"
        db_result = dump_database(db_config, db_dump)
        manifest["database"] = db_result
        if db_dump.exists() and db_dump.stat().st_size > 0:
            manifest["files"].append(file_entry(db_dump))

    manifest["valid"] = validate_manifest(manifest, backup_dir)
    write_json(backup_dir / "manifest.json", manifest)
    write_json(state_dir(path) / "last-backup.json", {"backup_dir": str(backup_dir), "manifest": manifest})
    write_docker_context(path)
    print(f"Backup criado: {backup_dir}")
    print("Status:", "VALIDADO" if manifest["valid"] else "NÃO VALIDADO")
    if not manifest["valid"]:
        raise SystemExit(1)


def make_plan(args: argparse.Namespace) -> None:
    path = Path(args.path).resolve()
    diag_path = Path(args.diagnostic or state_dir(path) / "diagnostic.json")
    if not diag_path.exists():
        raise SystemExit("Rode diagnose antes de gerar o plano.")
    diag = read_json(diag_path)
    version = diag.get("mautic_version") or ""
    install_type = diag.get("environment", {}).get("install_type", env_get("MSU_INSTALL_TYPE", "direct"))
    target_image = env_get("MSU_TARGET_IMAGE", "mautic/mautic:5")

    steps = [
        {"id": "confirmar_backup_servidor", "automatic": False, "description": "Confirmar snapshot/backup completo do servidor."},
        {"id": "backup_local_validado", "automatic": True, "description": "Criar e validar backup dos arquivos e banco quando possível."},
        {"id": "pausar_crons", "automatic": False, "description": "Pausar crons/workers externos do Mautic."},
        {"id": "verificar_plugins", "automatic": False, "description": "Validar plugins/temas customizados antes do Mautic 5+."},
    ]
    if version.startswith("3."):
        steps.append({"id": "migrar_3_para_4_4", "automatic": False, "description": "Mautic 3 deve migrar para 4.4 antes de 5+."})
    if install_type == "docker":
        steps += [
            {"id": "registrar_contexto_docker", "automatic": True, "description": "Salvar contexto Docker/Compose/Swarm em .msu/docker-context.json."},
            {"id": "trocar_imagem", "automatic": True, "description": f"Trocar somente a imagem do serviço Mautic para {target_image}."},
            {"id": "pos_upgrade_container", "automatic": True, "description": "Executar migrations, limpeza de cache e validação dentro do novo container."},
        ]
    else:
        steps += [
            {"id": "aplicar_upgrade_mautic", "automatic": False, "description": "Atualizar pacote/composer conforme ambiente."},
            {"id": "pos_upgrade", "automatic": True, "description": "Executar migrations, cache e validação."},
        ]
    steps.append({"id": "reativar_crons", "automatic": False, "description": "Reativar crons/workers após validação."})

    blockers = [r for r in diag.get("risks", []) if r["level"] == "BLOQUEADOR"]
    plan = {
        "target": args.target,
        "target_image": target_image if install_type == "docker" else None,
        "generated_at": now_stamp(),
        "install_type": install_type,
        "status": "bloqueado" if blockers else "pronto",
        "blockers": blockers,
        "steps": steps,
        "docker_context": docker_context_from_env(),
    }
    out = Path(args.output or state_dir(path) / "upgrade-plan.json")
    write_json(out, plan)
    write_docker_context(path)
    print(f"Plano gerado: {out}")
    print(f"Status: {plan['status']}")
    if install_type == "docker":
        print(f"Imagem alvo: {target_image}")


def print_upgrade_commands(path: Path) -> None:
    print("Comandos pós-upgrade seguros previstos:")
    print(f"cd {path}")
    print("php bin/console doctrine:migration:migrate --no-interaction")
    print("php bin/console cache:clear --no-warmup")
    print("php bin/console cache:warmup")
    print("php bin/console mautic:version")


def console_cmd_exists(path: Path, command: str) -> bool:
    result = run(["php", "bin/console", "list", "--raw"], cwd=path, timeout=60)
    return command in result.get("stdout", "")


def upgrade(args: argparse.Namespace) -> None:
    path = Path(args.path).resolve()
    confirm_or_fail(UPGRADE_PHRASE, args.confirm, "Upgrade bloqueado sem confirmação explícita.")
    last_backup = state_dir(path) / "last-backup.json"
    if not last_backup.exists():
        raise SystemExit("Upgrade bloqueado: nenhum backup validado encontrado.")
    backup_state = read_json(last_backup)
    if not backup_state.get("manifest", {}).get("valid"):
        raise SystemExit("Upgrade bloqueado: último backup não está validado.")

    if not args.execute:
        print("Simulação de upgrade. Use --execute para executar comandos automáticos seguros.")
        print_upgrade_commands(path)
        return

    steps: list[list[str]] = []
    if console_cmd_exists(path, "mautic:update:apply"):
        steps.append(["php", "bin/console", "mautic:update:apply", "--finish"])
    steps += [
        ["php", "bin/console", "doctrine:migration:migrate", "--no-interaction"],
        ["php", "bin/console", "cache:clear", "--no-warmup"],
        ["php", "bin/console", "cache:warmup"],
    ]

    events = []
    for cmd in steps:
        result = run(cmd, cwd=path, timeout=1800)
        events.append(result)
        print(result["cmd"], "OK" if result["ok"] else "FALHOU")
        if not result["ok"]:
            write_json(state_dir(path) / "upgrade-events.json", events)
            raise SystemExit("Falha crítica. Execute rollback ou corrija manualmente antes de continuar.")
    write_json(state_dir(path) / "upgrade-events.json", events)
    validate(argparse.Namespace(path=str(path), output=None))


def validate(args: argparse.Namespace) -> None:
    path = Path(args.path).resolve()
    checks = []
    for cmd in [["php", "bin/console", "--version"], ["php", "bin/console", "doctrine:migration:status"], ["php", "bin/console", "cache:clear", "--no-warmup"]]:
        checks.append(run(cmd, cwd=path, timeout=180))
    report = {"generated_at": now_stamp(), "checks": checks, "ok": all(c["ok"] for c in checks)}
    out = Path(args.output or state_dir(path) / "validation-report.json")
    write_json(out, report)
    print(f"Relatório de validação: {out}")
    print("Status:", "OK" if report["ok"] else "FALHOU")


def rollback(args: argparse.Namespace) -> None:
    path = Path(args.path).resolve()
    confirm_or_fail(ROLLBACK_PHRASE, args.confirm, "Rollback bloqueado sem confirmação explícita.")
    backup_dir = Path(args.backup).resolve() if args.backup else Path(read_json(state_dir(path) / "last-backup.json")["backup_dir"])
    manifest = read_json(backup_dir / "manifest.json")
    if not validate_manifest(manifest, backup_dir):
        raise SystemExit("Rollback bloqueado: manifesto do backup não validou.")
    if not args.execute:
        print("Simulação de rollback. Use --execute para restaurar arquivos.")
        print(f"Backup: {backup_dir}")
        print("Banco: restaure database.sql manualmente antes de liberar tráfego.")
        return
    archive = backup_dir / "mautic-files.tar.gz"
    restore_parent = path.parent
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name.startswith("/") or ".." in Path(member.name).parts:
                raise SystemExit("Backup contém caminho inseguro.")
        tar.extractall(restore_parent)
    print("Arquivos restaurados. Agora restaure o banco e rode validate antes de liberar o Mautic.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Mautic Safe Upgrade")
    p.add_argument("--path", default=".", help="Caminho do Mautic")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("diagnose")
    d.add_argument("--output")
    d.set_defaults(func=diagnose)

    b = sub.add_parser("backup")
    b.add_argument("--storage", default="./msu-backups")
    b.add_argument("--confirm")
    b.add_argument("--skip-db", action="store_true")
    b.set_defaults(func=create_backup)

    pl = sub.add_parser("plan")
    pl.add_argument("--diagnostic")
    pl.add_argument("--target", default="5.2-lts")
    pl.add_argument("--output")
    pl.set_defaults(func=make_plan)

    u = sub.add_parser("upgrade")
    u.add_argument("--confirm")
    u.add_argument("--execute", action="store_true")
    u.set_defaults(func=upgrade)

    v = sub.add_parser("validate")
    v.add_argument("--output")
    v.set_defaults(func=validate)

    r = sub.add_parser("rollback")
    r.add_argument("--backup")
    r.add_argument("--confirm")
    r.add_argument("--execute", action="store_true")
    r.set_defaults(func=rollback)
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
