#!/usr/bin/env python3
"""
Mautic Safe Upgrade - prototipo automatizado.

Foco: diagnosticar, exigir backup do servidor, criar backup local validado,
gerar plano, executar etapas seguras e permitir rollback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SERVER_BACKUP_PHRASE = "CONFIRMO QUE FIZ BACKUP DO SERVIDOR"
UPGRADE_PHRASE = "CONFIRMO UPGRADE"
ROLLBACK_PHRASE = "CONFIRMO ROLLBACK"


def now_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 60) -> dict[str, Any]:
    try:
        p = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
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


def sanitize(text: str) -> str:
    patterns = [
        (r"(?i)(password|passwd|pwd|secret|token|api[_-]?key)(\s*[:=]\s*)[^\s'\"]+", r"\1\2[REDACTED]"),
        (r"(?i)(Authorization:\s*Bearer\s+)[^\s]+", r"\1[REDACTED]"),
        (r"mysql://([^:]+):([^@]+)@", r"mysql://\1:[REDACTED]@"),
    ]
    for pattern, repl in patterns:
        text = re.sub(pattern, repl, text)
    return text


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


@dataclass
class Risk:
    level: str
    item: str
    message: str


@dataclass
class Context:
    mautic_path: Path
    storage: Path
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    @property
    def msu_dir(self) -> Path:
        return state_dir(self.mautic_path)


def detect_mautic_version(path: Path) -> str | None:
    forced = os.environ.get("MSU_DETECTED_MAUTIC_VERSION", "").strip()
    if forced:
        return forced

    console = path / "bin" / "console"
    if console.exists():
        for command in [
            ["php", str(console), "mautic:version"],
            ["php", str(console), "--version"],
        ]:
            result = run(command, cwd=path, timeout=30)
            text = f"{result.get('stdout', '')}\n{result.get('stderr', '')}"
            match = re.search(r"(\d+\.\d+(?:\.\d+)?)", text)
            if match:
                return match.group(1)

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
            match = re.search(r"(\d+\.\d+(?:\.\d+)?)", text)
            if match:
                return match.group(1)

    image = os.environ.get("MSU_DOCKER_IMAGE", "")
    if image:
        match = re.search(r"mautic[^:]*:v?(\d+(?:\.\d+)?(?:\.\d+)?)", image, flags=re.I)
        if match:
            version = match.group(1)
            if "." not in version:
                return f"{version}.x"
            return version

    return None


def detect_php() -> dict[str, Any]:
    result = run(["php", "-v"])
    version = None
    match = re.search(r"PHP\s+(\d+\.\d+\.\d+)", result.get("stdout", ""))
    if match:
        version = match.group(1)
    modules_result = run(["php", "-m"])
    modules = sorted({x.strip() for x in modules_result.get("stdout", "").splitlines() if x.strip() and not x.startswith("[")})
    return {"version": version, "raw": result, "modules": modules}


def detect_environment(path: Path) -> dict[str, Any]:
    files = {p.name for p in path.iterdir()} if path.exists() else set()
    parent_files = {p.name for p in path.parent.iterdir()} if path.parent.exists() else set()
    has_compose = bool({"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"} & (files | parent_files))
    docker = run(["docker", "ps", "--format", "{{.Names}}"], timeout=10)
    return {
        "path": str(path),
        "exists": path.exists(),
        "has_bin_console": (path / "bin" / "console").exists(),
        "docker_available": docker["ok"],
        "docker_compose_files": has_compose,
        "compose_candidates": sorted(list({"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"} & (files | parent_files))),
        "install_type": os.environ.get("MSU_INSTALL_TYPE", "direct"),
        "docker_container_id": os.environ.get("MSU_DOCKER_CONTAINER_ID", ""),
        "docker_container_name": os.environ.get("MSU_DOCKER_CONTAINER_NAME", ""),
        "docker_image": os.environ.get("MSU_DOCKER_IMAGE", ""),
        "docker_host_path": os.environ.get("MSU_DOCKER_HOST_PATH", ""),
        "docker_compose_project": os.environ.get("MSU_DOCKER_COMPOSE_PROJECT", ""),
        "docker_compose_service": os.environ.get("MSU_DOCKER_COMPOSE_SERVICE", ""),
        "docker_compose_files_detected": os.environ.get("MSU_DOCKER_COMPOSE_FILES", ""),
        "docker_compose_workdir": os.environ.get("MSU_DOCKER_COMPOSE_WORKDIR", ""),
        "plesk_hint": Path("/usr/local/psa").exists(),
        "cpanel_hint": Path("/usr/local/cpanel").exists(),
    }


def parse_local_config(path: Path) -> dict[str, str]:
    config_files = [
        path / "app" / "config" / "local.php",
        path / "config" / "local.php",
        path / ".env",
    ]
    found: dict[str, str] = {}
    for cfg in config_files:
        if not cfg.exists():
            continue
        text = cfg.read_text(errors="ignore", encoding="utf-8")
        for key in ["db_host", "db_port", "db_name", "db_user", "db_password"]:
            match = re.search(rf"['\"]{key}['\"]\s*=>\s*['\"]([^'\"]*)['\"]", text)
            if match:
                found[key] = match.group(1)
        for key, env_key in {
            "db_host": "MAUTIC_DB_HOST",
            "db_port": "MAUTIC_DB_PORT",
            "db_name": "MAUTIC_DB_NAME",
            "db_user": "MAUTIC_DB_USER",
            "db_password": "MAUTIC_DB_PASSWORD",
        }.items():
            match = re.search(rf"^{env_key}\s*=\s*(.+)$", text, re.MULTILINE)
            if match:
                found[key] = match.group(1).strip().strip("'\"")
    return found


def classify_risks(diag: dict[str, Any]) -> list[dict[str, str]]:
    risks: list[Risk] = []
    env = diag["environment"]
    php_version = diag["php"].get("version")
    mautic_version = diag.get("mautic_version")
    modules = set(diag["php"].get("modules", []))

    if not env["exists"]:
        risks.append(Risk("BLOQUEADOR", "Caminho Mautic", "O caminho informado não existe."))
    if not env["has_bin_console"]:
        risks.append(Risk("ALTO", "Console Mautic", "Não encontrei bin/console; upgrade automático completo pode não funcionar."))
    if not mautic_version:
        risks.append(Risk("ALTO", "Versão Mautic", "Não consegui detectar a versão atual do Mautic."))
    if not php_version:
        risks.append(Risk("BLOQUEADOR", "PHP CLI", "PHP CLI não foi encontrado ou não respondeu."))

    required_modules = {"xml", "curl", "gd", "mbstring", "intl", "zip", "bcmath"}
    missing = sorted(required_modules - modules)
    if missing:
        risks.append(Risk("ALTO", "Extensões PHP", "Extensões ausentes: " + ", ".join(missing)))

    if diag["disk"].get("free_gb", 0) < 5:
        risks.append(Risk("BLOQUEADOR", "Disco", "Menos de 5 GB livres; backup e upgrade podem falhar."))

    db = diag.get("database", {})
    if not db.get("db_name") or not db.get("db_user"):
        risks.append(Risk("ALTO", "Banco", "Credenciais do banco não foram detectadas automaticamente."))

    return [r.__dict__ for r in risks]


def diagnose(args: argparse.Namespace) -> None:
    path = Path(args.path).resolve()
    usage = shutil.disk_usage(path if path.exists() else path.parent)
    diag = {
        "generated_at": now_stamp(),
        "mautic_path": str(path),
        "mautic_version": detect_mautic_version(path),
        "php": detect_php(),
        "environment": detect_environment(path),
        "database": {k: ("[REDACTED]" if "password" in k else v) for k, v in parse_local_config(path).items()},
        "disk": {
            "total_gb": round(usage.total / 1024**3, 2),
            "free_gb": round(usage.free / 1024**3, 2),
        },
        "plugins": list_plugins(path),
        "themes": list_names(path / "themes") + list_names(path / "app" / "bundles"),
        "cron_hint": "Execute `crontab -l` no usuário do Mautic para confirmar crons externas.",
    }
    diag["risks"] = classify_risks(diag)
    out = Path(args.output or state_dir(path) / "diagnostic.json")
    write_json(out, diag)
    print(f"Diagnóstico gerado: {out}")
    print_summary(diag)


def list_names(path: Path) -> list[str]:
    if not path.exists() or not path.is_dir():
        return []
    return sorted([p.name for p in path.iterdir() if p.is_dir()])


def list_plugins(path: Path) -> list[str]:
    return list_names(path / "plugins") + list_names(path / "app" / "bundles")


def print_summary(diag: dict[str, Any]) -> None:
    print(f"Mautic: {diag.get('mautic_version') or 'não detectado'}")
    print(f"PHP CLI: {diag['php'].get('version') or 'não detectado'}")
    print(f"Disco livre: {diag['disk']['free_gb']} GB")
    for risk in diag.get("risks", []):
        print(f"[{risk['level']}] {risk['item']}: {risk['message']}")


def confirm_or_fail(phrase: str, provided: str | None, message: str) -> None:
    if provided == phrase:
        return
    print(message)
    print(f"Para continuar, rode novamente com: --confirm \"{phrase}\"")
    raise SystemExit(2)


def create_backup(args: argparse.Namespace) -> None:
    confirm_or_fail(
        SERVER_BACKUP_PHRASE,
        args.confirm,
        "Antes do backup local, faça um snapshot/backup completo do servidor, VPS ou volume no provedor. Isso protege contra falhas fora do Mautic.",
    )
    path = Path(args.path).resolve()
    storage = Path(args.storage).resolve()
    run_id = uuid.uuid4().hex[:8]
    backup_dir = storage / f"mautic-safe-upgrade-{now_stamp()}-{run_id}"
    backup_dir.mkdir(parents=True, mode=0o700, exist_ok=False)

    diag_path = state_dir(path) / "diagnostic.json"
    if not diag_path.exists():
        class Obj:
            pass
        dargs = Obj()
        dargs.path = str(path)
        dargs.output = str(diag_path)
        diagnose(dargs)

    manifest: dict[str, Any] = {
        "created_at": now_stamp(),
        "run_id": run_id,
        "mautic_path": str(path),
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
    print(f"Backup criado: {backup_dir}")
    print("Status:", "VALIDADO" if manifest["valid"] else "NÃO VALIDADO")
    if not manifest["valid"]:
        raise SystemExit(1)


def file_entry(path: Path) -> dict[str, Any]:
    return {"path": path.name, "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def dump_database(db: dict[str, str], out: Path) -> dict[str, Any]:
    needed = ["db_name", "db_user"]
    if any(not db.get(k) for k in needed):
        return {"ok": False, "message": "Credenciais insuficientes para dump automático."}
    cmd = ["mysqldump", "--single-transaction", "--routines", "--triggers"]
    if db.get("db_host"):
        cmd += ["-h", db["db_host"]]
    if db.get("db_port"):
        cmd += ["-P", db["db_port"]]
    cmd += ["-u", db["db_user"]]
    env = os.environ.copy()
    if db.get("db_password"):
        env["MYSQL_PWD"] = db["db_password"]
    cmd.append(db["db_name"])
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


def make_plan(args: argparse.Namespace) -> None:
    path = Path(args.path).resolve()
    diag_path = Path(args.diagnostic or state_dir(path) / "diagnostic.json")
    if not diag_path.exists():
        raise SystemExit("Rode diagnose antes de gerar o plano.")
    diag = read_json(diag_path)
    target = args.target
    steps = [
        {"id": "confirmar_backup_servidor", "automatic": False, "description": "Confirmar snapshot/backup completo do servidor."},
        {"id": "validar_backup_local", "automatic": True, "description": "Validar backup local criado pelo MSU."},
        {"id": "pausar_crons", "automatic": False, "description": "Pausar crons e workers de campanha/envio."},
        {"id": "desativar_plugins_incompativeis", "automatic": False, "description": "Desativar plugins sem compatibilidade com Mautic 5."},
    ]
    version = diag.get("mautic_version") or ""
    if version.startswith("3."):
        steps.append({"id": "migrar_3_para_4_4", "automatic": False, "description": "Migrar Mautic 3 para 4.4 antes do Mautic 5."})
    steps += [
        {"id": "ajustar_php", "automatic": False, "description": "Garantir PHP compatível com o destino."},
        {"id": "aplicar_upgrade_mautic", "automatic": False, "description": "Aplicar atualização por pacote/composer/container conforme ambiente."},
        {"id": "executar_migrations", "automatic": True, "description": "Executar migrations do banco."},
        {"id": "limpar_cache", "automatic": True, "description": "Limpar cache do Mautic."},
        {"id": "rebuild_assets", "automatic": True, "description": "Regerar assets quando comando existir."},
        {"id": "reativar_crons", "automatic": False, "description": "Reativar crons no PHP correto."},
        {"id": "validar_pos_upgrade", "automatic": True, "description": "Validar login básico, console, versão, cache e logs."},
    ]
    blockers = [r for r in diag.get("risks", []) if r["level"] == "BLOQUEADOR"]
    plan = {"target": target, "generated_at": now_stamp(), "status": "bloqueado" if blockers else "pronto", "blockers": blockers, "steps": steps}
    out = Path(args.output or state_dir(path) / "upgrade-plan.json")
    write_json(out, plan)
    print(f"Plano gerado: {out}")
    print(f"Status: {plan['status']}")


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

    steps = [
        ["php", "bin/console", "cache:clear"],
        ["php", "bin/console", "mautic:update:apply", "--finish"],
        ["php", "bin/console", "doctrine:migration:migrate", "--no-interaction"],
        ["php", "bin/console", "cache:clear"],
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


def print_upgrade_commands(path: Path) -> None:
    print("Comandos automáticos seguros previstos:")
    print(f"cd {path}")
    print("php bin/console cache:clear")
    print("php bin/console mautic:update:apply --finish")
    print("php bin/console doctrine:migration:migrate --no-interaction")
    print("php bin/console cache:clear")
    print("A atualização de pacote/composer/container deve seguir o plano do ambiente detectado.")


def validate(args: argparse.Namespace) -> None:
    path = Path(args.path).resolve()
    checks = []
    for cmd in [
        ["php", "bin/console", "mautic:version"],
        ["php", "bin/console", "doctrine:migration:status"],
        ["php", "bin/console", "cache:clear", "--no-warmup"],
    ]:
        checks.append(run(cmd, cwd=path, timeout=120))
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
        print("Banco: restaure database.sql manualmente ou com rotina controlada antes de liberar tráfego.")
        return
    archive = backup_dir / "mautic-files.tar.gz"
    restore_parent = path.parent
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name.startswith("/") or ".." in Path(member.name).parts:
                raise SystemExit("Backup contém caminho inseguro.")
        tar.extractall(restore_parent)
    print("Arquivos restaurados. Agora restaure o banco e rode validação antes de liberar o Mautic.")


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
