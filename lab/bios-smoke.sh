#!/usr/bin/env bash
# Disposable SeaBIOS/MBR smoke without touching a host disk.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
REPORT_DIR="${PYFOG_BIOS_REPORT_DIR:-$PROJECT_ROOT/.e2e}"
WORK_DIR=""
QEMU_PID=""

die() {
    printf 'bios-smoke: error: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Uso: ./lab/bios-smoke.sh [doctor|run]

doctor comprueba QEMU, qemu-img, sfdisk, Python y SeaBIOS.
run crea un disco raw MBR descartable, arranca su sector 0 con SeaBIOS y
verifica una marca serial emitida por el código de arranque.
EOF
}

seabios_path() {
    local candidate
    for candidate in /usr/share/seabios/bios.bin /usr/share/qemu/bios.bin; do
        if [[ -r "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

doctor() {
    local missing=0 tool
    for tool in qemu-img qemu-system-x86_64 sfdisk python3; do
        if command -v "$tool" >/dev/null 2>&1; then
            printf 'OK       %s\n' "$tool"
        else
            printf 'FALTA    %s\n' "$tool" >&2
            missing=1
        fi
    done
    if seabios_path >/dev/null; then
        printf 'OK       SeaBIOS\n'
    else
        printf 'FALTA    SeaBIOS\n' >&2
        missing=1
    fi
    if ((missing)); then
        printf '%s\n' 'En Ubuntu: sudo apt-get install qemu-system-x86 qemu-utils fdisk' >&2
        return 1
    fi
}

cleanup() {
    if [[ -n "$QEMU_PID" ]] && kill -0 "$QEMU_PID" 2>/dev/null; then
        kill -TERM "$QEMU_PID" 2>/dev/null || true
        wait "$QEMU_PID" 2>/dev/null || true
    fi
    if [[ -n "$WORK_DIR" && "$WORK_DIR" != "/" && -d "$WORK_DIR" ]]; then
        rm -rf -- "$WORK_DIR"
    fi
}

create_mbr_disk() {
    local disk="$1"
    qemu-img create -q -f raw "$disk" 64M
    sfdisk --wipe always --no-reread "$disk" <<'EOF'
label: dos
label-id: 0x1a2b3c4d
unit: sectors
sector-size: 512

start=2048, size=129024, type=83, bootable
EOF
    python3 - "$disk" <<'PY'
import struct
import sys
from pathlib import Path

disk = Path(sys.argv[1])
code = bytearray(
    bytes.fromhex(
        "31c08ed88ed0bc007c"
        "bafb03b080eeba f803b001eeba fb03b003eeba fc03b003ee".replace(" ", "")
    )
)
loop = len(code)
code.extend(b"\xbe\x00\x00\xac\x88\xc4\x84\xc0\x74\x00")
wait = len(code)
code.extend(b"\xba\xfd\x03\xec\xa8\x20\x74\x00\x88\xe0\xba\xf8\x03\xee")
code.extend(b"\xeb\x00")
halt = len(code)
code.extend(b"\xfa\xf4\xeb\xfd")
message = b"PYFOG_BIOS_MBR_OK\r\n\0"
message_offset = 0x7C00 + len(code)
struct.pack_into("<H", code, loop + 1, message_offset)
code[loop + 9] = (halt - (loop + 10)) & 0xFF
code[wait + 7] = (wait - (wait + 9)) & 0xFF
code[halt - 1] = (loop + 3 - halt) & 0xFF
code.extend(message)
if len(code) > 446:
    raise SystemExit("boot code exceeds the MBR code area")
code.extend(b"\0" * (446 - len(code)))
with disk.open("r+b") as target:
    target.seek(0)
    target.write(code)
    target.flush()
PY
    if [[ "$(dd if="$disk" bs=1 skip=510 count=2 status=none | od -An -tx1 | tr -d ' \n')" != "55aa" ]]; then
        die "sfdisk no conservó la firma MBR"
    fi
}

run_smoke() {
    local bios disk serial qemu_log elapsed=0
    doctor
    mkdir -p "$REPORT_DIR"
    chmod 700 "$REPORT_DIR"
    WORK_DIR="$(mktemp -d "$REPORT_DIR/.bios-smoke.XXXXXX")"
    chmod 700 "$WORK_DIR"
    disk="$WORK_DIR/bios-mbr.raw"
    serial="$WORK_DIR/serial.log"
    qemu_log="$WORK_DIR/qemu.log"
    bios="$(seabios_path)"
    create_mbr_disk "$disk"
    qemu-system-x86_64 \
        -name pyfog-bios-mbr-smoke \
        -machine q35,accel=tcg \
        -cpu max \
        -m 128 \
        -display none \
        -no-reboot \
        -bios "$bios" \
        -drive file="$disk",format=raw,if=ide \
        -boot order=c,strict=on \
        -serial "file:$serial" \
        >"$qemu_log" 2>&1 &
    QEMU_PID=$!
    while ((elapsed < 15)); do
        if grep -Fq 'PYFOG_BIOS_MBR_OK' "$serial" 2>/dev/null; then
            cp -- "$serial" "$REPORT_DIR/bios.serial.log"
            cp -- "$qemu_log" "$REPORT_DIR/bios.qemu.log"
            chmod 600 "$REPORT_DIR/bios.serial.log" "$REPORT_DIR/bios.qemu.log"
            printf 'BIOS/MBR smoke PASS\n'
            printf 'serial_log=%s\nqemu_log=%s\n' \
                "$REPORT_DIR/bios.serial.log" "$REPORT_DIR/bios.qemu.log"
            return 0
        fi
        if ! kill -0 "$QEMU_PID" 2>/dev/null; then
            wait "$QEMU_PID" || true
            QEMU_PID=""
            break
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
    [[ -f "$serial" ]] && cp -- "$serial" "$REPORT_DIR/bios.serial.log"
    [[ -f "$qemu_log" ]] && cp -- "$qemu_log" "$REPORT_DIR/bios.qemu.log"
    chmod 600 "$REPORT_DIR/bios.serial.log" "$REPORT_DIR/bios.qemu.log" 2>/dev/null || true
    printf 'BIOS/MBR smoke FAIL\n' >&2
    [[ -f "$serial" ]] && sed -n '1,80p' "$serial" >&2 || true
    [[ -f "$qemu_log" ]] && sed -n '1,80p' "$qemu_log" >&2 || true
    return 1
}

trap cleanup EXIT
case "${1:-}" in
    doctor)
        (($# == 1)) || die "doctor no recibe argumentos"
        doctor
        ;;
    run)
        (($# == 1)) || die "run no recibe argumentos"
        run_smoke
        ;;
    help | --help | -h)
        usage
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
