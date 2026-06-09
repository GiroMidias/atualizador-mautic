#!/usr/bin/env bash

set -Eeuo pipefail

RAW_BASE="${MSU_RAW_BASE:-https://raw.githubusercontent.com/GiroMidias/atualizador-mautic/main}"
UPDATE_URL="${MSU_UPDATE_URL:-$RAW_BASE/update.py}"
TARGET="${MSU_TARGET:-5.2-lts}"
EXECUTE="${MSU_EXECUTE:-0}"
SERVER_BACKUP="${MSU_SERVER_BACKUP:-0}"
STORAGE_PATH="${MSU_STORAGE:-/root/msu-backups}"
TMP_DIR="/tmp/msu-$$"
UPDATE_FILE="$TMP_DIR/update.py"
CANDIDATES="$TMP_DIR/candidates.tsv"

mkdir -p "$TMP_DIR"
touch "$CANDIDATES"

log() {
  echo ""
  echo "$1"
}

fail() {
  echo "ERRO: $1"
  exit 1
}

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

download_update() {
  log "Baixando update.py..."

  if has_cmd curl; then
    curl -fsSL -o "$UPDATE_FILE" "$UPDATE_URL"
  elif has_cmd wget; then
    wget -q -O "$UPDATE_FILE" "$UPDATE_URL"
  else
    fail "curl ou wget não encontrado. Não vou instalar nada automaticamente."
  fi

  chmod +x "$UPDATE_FILE"
}

extract_version() {
  grep -Eo '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -n 1 || true
}

detect_version_from_image() {
  local image="$1"

  echo "$image" | grep -Eoi 'mautic[^:]*:v?[0-9]+(\.[0-9]+)?(\.[0-9]+)?' \
    | grep -Eo '[0-9]+(\.[0-9]+)?(\.[0-9]+)?' \
    | head -n 1 || true
}

detect_version_inside_container() {
  local cid="$1"
  local cpath="$2"
  local image="$3"
  local version=""

  version=$(docker exec "$cid" sh -lc "cd '$cpath' && php bin/console mautic:version 2>/dev/null" | extract_version || true)

  if [ -z "$version" ]; then
    version=$(docker exec "$cid" sh -lc "cd '$cpath' && php bin/console --version 2>/dev/null" | extract_version || true)
  fi

  if [ -z "$version" ]; then
    version=$(docker exec "$cid" sh -lc "
      cd '$cpath'

      for file in \
        app/version.txt \
        VERSION.txt \
        version.txt \
        app/bundles/CoreBundle/Version.php \
        app/bundles/CoreBundle/ReleaseMetadata.php \
        composer.json \
        composer.lock
      do
        if [ -f \"\$file\" ]; then
          grep -Eo '[0-9]+\.[0-9]+(\.[0-9]+)?' \"\$file\" 2>/dev/null | head -n 1 && exit 0
        fi
      done
    " 2>/dev/null || true)
  fi

  if [ -z "$version" ]; then
    version=$(detect_version_from_image "$image")
  fi

  if [ -z "$version" ]; then
    version="desconhecida"
  fi

  echo "$version"
}

find_path_inside_container() {
  local cid="$1"

  docker exec "$cid" sh -lc '
    for path in /var/www/html /var/www/html/docroot /var/www/mautic /app /srv/app /srv/mautic; do
      if [ -f "$path/bin/console" ]; then
        echo "$path"
        exit 0
      fi
    done

    find /var/www /app /srv -maxdepth 7 -type f -path "*/bin/console" 2>/dev/null \
      | head -n 1 \
      | while read console; do dirname "$(dirname "$console")"; done
  ' 2>/dev/null || true
}

map_container_path_to_host() {
  local cid="$1"
  local cpath="$2"
  local best_source=""
  local best_dest=""
  local best_len=0
  local src dst len rel

  while IFS='|' read -r src dst; do
    [ -n "${src:-}" ] || continue
    [ -n "${dst:-}" ] || continue

    case "$cpath" in
      "$dst"|"$dst"/*)
        len=${#dst}
        if [ "$len" -gt "$best_len" ]; then
          best_len="$len"
          best_source="$src"
          best_dest="$dst"
        fi
        ;;
    esac
  done < <(docker inspect -f '{{range .Mounts}}{{println .Source "|" .Destination}}{{end}}' "$cid" 2>/dev/null || true)

  if [ -n "$best_source" ]; then
    rel="${cpath#$best_dest}"
    echo "${best_source}${rel}"
  fi
}

clean_value() {
  echo "${1:-}" | sed 's/<no value>//g'
}

add_candidate() {
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$1" "$2" "$3" "$4" "$5" "$6" "$7" "$8" "$9" "${10}" "${11}" "${12}" >> "$CANDIDATES"
}

score_local_path() {
  local path="$1"
  local score=0
  local version="desconhecida"

  [ -f "$path/bin/console" ] || return 0
  score=$((score + 10))

  if [ -f "$path/app/config/local.php" ] || [ -f "$path/config/local.php" ] || [ -f "$path/.env" ]; then
    score=$((score + 3))
  fi

  if [ -f "$path/composer.json" ] && grep -qi 'mautic' "$path/composer.json" 2>/dev/null; then
    score=$((score + 3))
  fi

  if has_cmd php; then
    version=$(cd "$path" && php bin/console mautic:version 2>/dev/null | extract_version || true)

    if [ -z "$version" ]; then
      version=$(cd "$path" && php bin/console --version 2>/dev/null | extract_version || true)
    fi

    if [ -z "$version" ]; then
      for file in \
        "$path/app/version.txt" \
        "$path/VERSION.txt" \
        "$path/version.txt" \
        "$path/app/bundles/CoreBundle/Version.php" \
        "$path/app/bundles/CoreBundle/ReleaseMetadata.php" \
        "$path/composer.json" \
        "$path/composer.lock"
      do
        if [ -f "$file" ]; then
          version=$(grep -Eo '[0-9]+\.[0-9]+(\.[0-9]+)?' "$file" 2>/dev/null | head -n 1 || true)
          [ -n "$version" ] && break
        fi
      done
    fi

    if [ -n "$version" ]; then
      score=$((score + 20))
    else
      version="desconhecida"
    fi
  fi

  add_candidate "$score" "direct" "$path" "$version" "" "" "" "" "" "" "" ""
}

find_direct_installations() {
  log "Procurando instalação direta..."

  if [ "${1:-}" != "" ]; then
    local manual_path
    manual_path=$(readlink -f "$1" 2>/dev/null || true)

    if [ -n "$manual_path" ]; then
      score_local_path "$manual_path"
    fi
  fi

  local roots=(
    "/var/www"
    "/home"
    "/srv"
    "/opt"
    "/usr/share/nginx"
    "/var/www/vhosts"
    "/var/lib/docker/volumes"
  )

  local root console path

  for root in "${roots[@]}"; do
    [ -d "$root" ] || continue

    while IFS= read -r console; do
      path=$(dirname "$(dirname "$console")")
      score_local_path "$path"
    done < <(find "$root" -maxdepth 9 -type f -path '*/bin/console' 2>/dev/null | sort -u)
  done
}

find_docker_installations() {
  log "Procurando instalação Docker..."

  if ! has_cmd docker; then
    echo "Docker não encontrado no host. Pulando detecção Docker."
    return 0
  fi

  if ! docker ps >/dev/null 2>&1; then
    echo "Docker existe, mas este usuário não consegue acessar docker ps. Pulando detecção Docker."
    return 0
  fi

  local cid name image cpath version host_path project service config_files workdir score

  for cid in $(docker ps -q); do
    name=$(docker inspect -f '{{.Name}}' "$cid" 2>/dev/null | sed 's#^/##' || true)
    image=$(docker inspect -f '{{.Config.Image}}' "$cid" 2>/dev/null || true)
    cpath=$(find_path_inside_container "$cid")

    if [ -z "$cpath" ]; then
      continue
    fi

    version=$(detect_version_inside_container "$cid" "$cpath" "$image")
    host_path=$(map_container_path_to_host "$cid" "$cpath")

    project=$(clean_value "$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$cid" 2>/dev/null || true)")
    service=$(clean_value "$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.service" }}' "$cid" 2>/dev/null || true)")
    config_files=$(clean_value "$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' "$cid" 2>/dev/null || true)")
    workdir=$(clean_value "$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$cid" 2>/dev/null || true)")

    score=30

    echo "$name $image" | grep -qi mautic && score=$((score + 20))
    [ "$version" != "desconhecida" ] && score=$((score + 10))
    [ -n "$host_path" ] && score=$((score + 5))
    [ -n "$project" ] && score=$((score + 5))

    add_candidate "$score" "docker" "$cpath" "$version" "$cid" "$name" "$image" "$host_path" "$project" "$service" "$config_files" "$workdir"
  done
}

select_best_candidate() {
  if [ ! -s "$CANDIDATES" ]; then
    fail "Não encontrei o Mautic automaticamente em instalação direta nem Docker."
  fi

  sort -t $'\t' -k1,1nr "$CANDIDATES" | head -n 1
}

run_direct_flow() {
  local path="$1"
  local version="$2"

  has_cmd python3 || fail "python3 não encontrado no host. Não vou instalar nada automaticamente."

  log "Rodando diagnóstico direto..."
  MSU_INSTALL_TYPE="direct" \
  MSU_DETECTED_MAUTIC_VERSION="$version" \
  python3 "$UPDATE_FILE" --path "$path" diagnose

  log "Gerando plano..."
  MSU_INSTALL_TYPE="direct" \
  MSU_DETECTED_MAUTIC_VERSION="$version" \
  python3 "$UPDATE_FILE" --path "$path" plan --target "$TARGET"

  if [ "$EXECUTE" = "1" ]; then
    [ "$SERVER_BACKUP" = "1" ] || fail "Para executar upgrade, rode com MSU_SERVER_BACKUP=1 depois de fazer snapshot/backup do servidor."

    log "Criando backup local..."
    MSU_INSTALL_TYPE="direct" \
    MSU_DETECTED_MAUTIC_VERSION="$version" \
    python3 "$UPDATE_FILE" --path "$path" backup \
      --storage "$STORAGE_PATH" \
      --confirm "CONFIRMO QUE FIZ BACKUP DO SERVIDOR"

    log "Executando upgrade..."
    MSU_INSTALL_TYPE="direct" \
    MSU_DETECTED_MAUTIC_VERSION="$version" \
    python3 "$UPDATE_FILE" --path "$path" upgrade \
      --confirm "CONFIRMO UPGRADE" \
      --execute
  else
    log "Modo seguro: não executei upgrade."
    echo "Para executar depois, use MSU_EXECUTE=1 e MSU_SERVER_BACKUP=1."
  fi
}

run_docker_flow() {
  local cpath="$1"
  local cid="$2"
  local name="$3"
  local image="$4"
  local host_path="$5"
  local project="$6"
  local service="$7"
  local config_files="$8"
  local workdir="$9"
  local version="${10}"

  log "Rodando diagnóstico Docker..."
  echo "Container: $name"
  echo "Imagem: $image"
  echo "Caminho no container: $cpath"
  echo "Versão detectada: $version"
  [ -n "$host_path" ] && echo "Caminho no host: $host_path"
  [ -n "$project" ] && echo "Docker Compose project: $project"
  [ -n "$service" ] && echo "Docker Compose service: $service"
  [ -n "$config_files" ] && echo "Compose file: $config_files"
  [ -n "$workdir" ] && echo "Compose dir: $workdir"

  echo "PHP: $(docker exec "$cid" sh -lc "php -v 2>/dev/null | head -n 1" || true)"

  if docker exec "$cid" sh -lc 'command -v python3 >/dev/null 2>&1' >/dev/null 2>&1; then
    docker cp "$UPDATE_FILE" "$cid:/tmp/update.py" >/dev/null
    docker exec "$cid" chmod +x /tmp/update.py

    log "Gerando diagnóstico completo dentro do container..."
    docker exec \
      -e MSU_INSTALL_TYPE="docker" \
      -e MSU_DETECTED_MAUTIC_VERSION="$version" \
      -e MSU_DOCKER_CONTAINER_ID="$cid" \
      -e MSU_DOCKER_CONTAINER_NAME="$name" \
      -e MSU_DOCKER_IMAGE="$image" \
      -e MSU_DOCKER_HOST_PATH="$host_path" \
      -e MSU_DOCKER_COMPOSE_PROJECT="$project" \
      -e MSU_DOCKER_COMPOSE_SERVICE="$service" \
      -e MSU_DOCKER_COMPOSE_FILES="$config_files" \
      -e MSU_DOCKER_COMPOSE_WORKDIR="$workdir" \
      "$cid" python3 /tmp/update.py --path "$cpath" diagnose || true

    log "Gerando plano dentro do container..."
    docker exec \
      -e MSU_INSTALL_TYPE="docker" \
      -e MSU_DETECTED_MAUTIC_VERSION="$version" \
      -e MSU_DOCKER_CONTAINER_ID="$cid" \
      -e MSU_DOCKER_CONTAINER_NAME="$name" \
      -e MSU_DOCKER_IMAGE="$image" \
      -e MSU_DOCKER_HOST_PATH="$host_path" \
      -e MSU_DOCKER_COMPOSE_PROJECT="$project" \
      -e MSU_DOCKER_COMPOSE_SERVICE="$service" \
      -e MSU_DOCKER_COMPOSE_FILES="$config_files" \
      -e MSU_DOCKER_COMPOSE_WORKDIR="$workdir" \
      "$cid" python3 /tmp/update.py --path "$cpath" plan --target "$TARGET" || true
  else
    echo "python3 não existe dentro do container. Não vou instalar nada nele."
  fi

  log "Diagnóstico Docker concluído."
  echo "O próximo passo é implementar o upgrade Docker-aware no update.py sem alterar nada fora do Mautic."

  if [ "$EXECUTE" = "1" ]; then
    fail "Upgrade automático Docker ainda está bloqueado para evitar trocar imagem/compose errado."
  fi
}

main() {
  log "Mautic Safe Upgrade"
  echo "Sem apt install, sem apt update, sem alteração no Docker do servidor."

  download_update
  find_direct_installations "${1:-}"
  find_docker_installations

  local best score kind path version cid name image host_path project service config_files workdir
  best=$(select_best_candidate)

  IFS=$'\t' read -r score kind path version cid name image host_path project service config_files workdir <<< "$best"

  log "Mautic encontrado"
  echo "Tipo: $kind"
  echo "Versão: $version"
  echo "Caminho: $path"

  if [ "$kind" = "direct" ]; then
    run_direct_flow "$path" "$version"
  elif [ "$kind" = "docker" ]; then
    run_docker_flow "$path" "$cid" "$name" "$image" "$host_path" "$project" "$service" "$config_files" "$workdir" "$version"
  else
    fail "Tipo desconhecido: $kind"
  fi
}

main "${1:-}"
