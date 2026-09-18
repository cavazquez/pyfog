#!/usr/bin/env bash
# Reproducible contract and UEFI smoke runner for the disposable PyFog laboratory.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
LAB_DRIVER="$SCRIPT_DIR/pyfog-lab"
REPORT_ROOT="${PYFOG_E2E_REPORT_DIR:-$PROJECT_ROOT/.e2e}"
RUN_DIR=""
QEMU_STARTED=0

die() {
    printf 'e2e: error: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Uso: ./lab/e2e.sh COMANDO

Comandos:
  plan    Imprime los escenarios y herramientas requeridas, sin modificar nada.
  run     Ejecuta el contrato agente/coordinador y el smoke UEFI en .lab/.

El comando run conserva un informe en .e2e/run.*. No acepta tokens en argumentos ni los
imprime. Las VMs sólo usan los overlays marcados por lab/pyfog-lab; no se pasan discos del host.
Después de revisar el informe, limpiá el laboratorio con ./lab/pyfog-lab destroy.
EOF
}

plan() {
    cat <<'EOF'
PyFog E2E reproducible

1. source A: contrato de inventario, captura, manifiesto y restauración verificada.
2. source A: se altera el estado esperado; restore vuelve a validar layout, archivos y UEFI.
3. target B: clone desde la misma imagen y comprueba hostname, machine-id, SSH y DHCP independientes.
4. negativos: manifiesto/artefacto corrupto, disco menor, doble reserva, lease/cancelación,
   corte de red y reinicio del coordinador sin segundo escritor.
5. UEFI: arranca source y target con OVMF sin Secure Boot sobre overlays QCOW2 descartables.

El contrato se ejecuta con tests/test_e2e_contract.py y las pruebas unitarias de restauración/tareas.
El smoke UEFI se ejecuta con lab/pyfog-lab; los logs se guardan sin credenciales.

Herramientas: uv, QEMU/qemu-img, OVMF, cloud-localds, gdisk/sgdisk, file, socat y curl.
EOF
}

validate_report_root() {
    [[ -n "$REPORT_ROOT" && "$REPORT_ROOT" != "/" && "$REPORT_ROOT" != "$PROJECT_ROOT" ]] ||
        die "PYFOG_E2E_REPORT_DIR apunta a una ruta demasiado amplia."
    [[ ! -L "$REPORT_ROOT" ]] || die "El directorio de informes no puede ser un enlace simbólico."
}

prepare_report() {
    validate_report_root
    mkdir -p "$REPORT_ROOT"
    chmod 700 "$REPORT_ROOT"
    RUN_DIR="$(mktemp -d "$REPORT_ROOT/run.XXXXXX")"
    chmod 700 "$RUN_DIR"
    exec > >(tee "$RUN_DIR/console.log") 2>&1
    {
        printf 'format=pyfog-e2e-report-v1\n'
        printf 'commit='
        git -C "$PROJECT_ROOT" rev-parse --short HEAD
        printf 'started_at='
        date -u +%Y-%m-%dT%H:%M:%SZ
        printf 'python='
        python3 --version
        printf 'platform='
        uname -srmo
        printf 'qemu='
        if command -v qemu-system-x86_64 >/dev/null 2>&1; then
            qemu-system-x86_64 --version | head -n 1
        else
            printf 'unavailable\n'
        fi
    } >"$RUN_DIR/metadata.env"
    printf 'status=running\n' >"$RUN_DIR/result.env"
    printf 'Informe: %s\n' "$RUN_DIR"
}

record() {
    local scenario="$1" result="$2" detail="$3"
    printf '%s\t%s\t%s\n' "$scenario" "$result" "$detail" >>"$RUN_DIR/scenarios.tsv"
    printf '%-28s %-7s %s\n' "$scenario" "$result" "$detail"
}

run_contract() {
    local test_log="$RUN_DIR/contract.log"
    if UV_CACHE_DIR="${UV_CACHE_DIR:-$PROJECT_ROOT/.uv-cache}" uv run pytest -q \
        tests/test_e2e_contract.py tests/test_tasks.py tests/test_restore.py \
        tests/test_tasking_unit.py tests/test_image_manifest.py \
        >"$test_log" 2>&1; then
        record "agente-coordinador" "PASS" "contrato de operaciones y negativos"
    else
        record "agente-coordinador" "FAIL" "ver contract.log"
        return 1
    fi
}

stop_lab_on_exit() {
    if ((QEMU_STARTED)) && [[ -f "$PROJECT_ROOT/.lab/.pyfog-lab-marker" ]]; then
        "$LAB_DRIVER" stop all || true
    fi
}

run_qemu() {
    QEMU_STARTED=1
    if "$LAB_DRIVER" up >"$RUN_DIR/qemu.log" 2>&1; then
        "$LAB_DRIVER" status >>"$RUN_DIR/qemu.log" 2>&1
        for vm in source target; do
            [[ -s "$PROJECT_ROOT/.lab/$vm/serial.log" ]] || {
                record "uefi-$vm" "FAIL" "serial.log vacío"
                return 1
            }
            record "uefi-$vm" "PASS" "OVMF/QEMU activo; ver qemu.log y serial.log"
        done
        return 0
    fi
    record "uefi" "FAIL" "ver qemu.log"
    return 1
}

run_all() {
    trap stop_lab_on_exit EXIT
    prepare_report
    if ! command -v uv >/dev/null 2>&1; then
        record "preflight" "FAIL" "falta uv"
        return 1
    fi
    if ! "$LAB_DRIVER" doctor >"$RUN_DIR/doctor.log" 2>&1; then
        record "preflight" "FAIL" "ver doctor.log"
        printf 'status=blocked\n' >"$RUN_DIR/result.env"
        return 1
    fi
    record "preflight" "PASS" "herramientas QEMU/OVMF disponibles"
    if ! run_contract; then
        printf 'status=failed\n' >"$RUN_DIR/result.env"
        return 1
    fi
    if ! run_qemu; then
        printf 'status=failed\n' >"$RUN_DIR/result.env"
        return 1
    fi
    printf 'status=passed\n' >"$RUN_DIR/result.env"
    printf 'E2E PASS. Revisá %s y ejecutá lab/pyfog-lab destroy cuando termine la revisión.\n' "$RUN_DIR"
}

command_name="${1:-}"
case "$command_name" in
    plan)
        (($# == 1)) || die "plan no recibe argumentos."
        plan
        ;;
    run)
        (($# == 1)) || die "run no recibe argumentos."
        run_all
        ;;
    help | --help | -h)
        usage
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
