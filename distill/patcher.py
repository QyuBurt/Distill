"""Applying IPS, UPS, BPS and xdelta (VCDIFF) patches, with checksum checks. No Qt in here.

IPS has no checksums, so only the result's size is sanity-checked. UPS and BPS carry CRC32s of the source, the target
and the patch itself, and all three are verified. xdelta is handed to the `xdelta3` program (found on PATH)."""
from __future__ import annotations

import shutil
import subprocess
import zlib
from pathlib import Path

from .constants import SUBPROCESS_FLAGS

PATCH_EXTENSIONS = frozenset({".ips", ".ups", ".bps", ".xdelta", ".xdelta3", ".vcdiff"})


class PatchError(Exception):
    pass


def _crc(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def detect(patch: Path) -> str:
    """'ips' | 'ups' | 'bps' | 'xdelta' by the file's header, falling back to its extension; '' if unknown."""
    try:
        with patch.open("rb") as f:
            head = f.read(5)
    except OSError:
        return ""
    if head[:5] == b"PATCH":
        return "ips"
    if head[:4] == b"UPS1":
        return "ups"
    if head[:4] == b"BPS1":
        return "bps"
    if head[:3] == b"\xd6\xc3\xc4":  # VCDIFF magic
        return "xdelta"
    ext = patch.suffix.lower()
    return "xdelta" if ext in (".xdelta", ".xdelta3", ".vcdiff") else ext.lstrip(".") if ext in (".ips", ".ups", ".bps") else ""


class _Reader:
    def __init__(self, data: bytes, pos: int = 0):
        self.d, self.p = data, pos

    def byte(self) -> int:
        if self.p >= len(self.d):
            raise PatchError("Patch file is truncated")
        b = self.d[self.p]
        self.p += 1
        return b

    def number(self) -> int:  # the variable-length number shared by UPS and BPS
        value, shift = 0, 1
        while True:
            x = self.byte()
            value += (x & 0x7F) * shift
            if x & 0x80:
                return value
            shift <<= 7
            value += shift


def apply_ips(src: bytes, patch: bytes) -> tuple[bytes, list[str]]:
    if patch[:5] != b"PATCH":
        raise PatchError("Not an IPS patch")
    out = bytearray(src)
    p, n = 5, len(patch)
    while True:
        if p + 3 > n:
            raise PatchError("IPS patch has no end marker")
        if patch[p:p + 3] == b"EOF":
            p += 3
            break
        off = int.from_bytes(patch[p:p + 3], "big")
        size = int.from_bytes(patch[p + 3:p + 5], "big")
        p += 5
        if size == 0:  # run-length record
            count = int.from_bytes(patch[p:p + 2], "big")
            chunk = patch[p + 2:p + 3] * count
            p += 3
        else:
            chunk = patch[p:p + size]
            if len(chunk) != size:
                raise PatchError("IPS patch is truncated")
            p += size
        if off > len(out):
            out.extend(b"\0" * (off - len(out)))
        out[off:off + len(chunk)] = chunk
    if p + 3 <= n:  # optional truncation length
        out = out[:int.from_bytes(patch[p:p + 3], "big")]
    return bytes(out), ["IPS has no checksums: result not verified"]


def _footer(patch: bytes) -> tuple[int, int, int]:
    if len(patch) < 12:
        raise PatchError("Patch file is truncated")
    tail = patch[-12:]
    return tuple(int.from_bytes(tail[4 * i:4 * i + 4], "little") for i in range(3))  # type: ignore[return-value]


def apply_bps(src: bytes, patch: bytes) -> tuple[bytes, list[str]]:
    if patch[:4] != b"BPS1":
        raise PatchError("Not a BPS patch")
    src_crc, tgt_crc, patch_crc = _footer(patch)
    if _crc(patch[:-4]) != patch_crc:
        raise PatchError("Patch file is damaged (its own checksum fails)")
    if _crc(src) != src_crc:
        raise PatchError(f"Wrong source file: checksum {_crc(src):08x}, the patch expects {src_crc:08x}")
    r = _Reader(patch[:-12], 4)
    src_size, tgt_size, meta = r.number(), r.number(), r.number()
    r.p += meta
    if src_size != len(src):
        raise PatchError("Wrong source file: size differs from what the patch expects")
    out = bytearray(tgt_size)
    o = s_rel = t_rel = 0
    end = len(r.d)
    while r.p < end:
        data = r.number()
        length, mode = (data >> 2) + 1, data & 3
        if o + length > tgt_size:
            raise PatchError("Patch writes past the end of the target")
        if mode == 0:
            out[o:o + length] = src[o:o + length]
            o += length
        elif mode == 1:
            out[o:o + length] = r.d[r.p:r.p + length]
            r.p += length
            o += length
        else:
            d = r.number()
            delta = -(d >> 1) if d & 1 else d >> 1
            if mode == 2:
                s_rel += delta
                out[o:o + length] = src[s_rel:s_rel + length]
                s_rel += length
                o += length
            else:
                t_rel += delta
                for _ in range(length):  # may overlap what it is writing, so byte by byte
                    out[o] = out[t_rel]
                    o += 1
                    t_rel += 1
    if _crc(out) != tgt_crc:
        raise PatchError("Result checksum doesn't match: wrong source or damaged patch")
    return bytes(out), ["Source, patch and result checksums verified"]


def apply_ups(src: bytes, patch: bytes) -> tuple[bytes, list[str]]:
    if patch[:4] != b"UPS1":
        raise PatchError("Not a UPS patch")
    src_crc, tgt_crc, patch_crc = _footer(patch)
    if _crc(patch[:-4]) != patch_crc:
        raise PatchError("Patch file is damaged (its own checksum fails)")
    r = _Reader(patch[:-12], 4)
    in_size, out_size = r.number(), r.number()
    crc = _crc(src)
    if crc == src_crc:
        size = out_size
    elif crc == tgt_crc:  # UPS patches are symmetrical: this undoes the patch
        size = in_size
    else:
        raise PatchError(f"Wrong source file: checksum {crc:08x}, the patch expects {src_crc:08x}")
    out = bytearray(src[:size].ljust(size, b"\0"))
    pos = 0
    end = len(r.d)
    while r.p < end:
        pos += r.number()
        while True:
            x = r.byte()
            if x == 0:
                break
            if pos < size:
                out[pos] ^= x
            pos += 1
        pos += 1
    want = tgt_crc if crc == src_crc else src_crc
    if _crc(out) != want:
        raise PatchError("Result checksum doesn't match: damaged patch")
    return bytes(out), ["Source, patch and result checksums verified" + ("" if crc == src_crc else " (patch reversed)")]


def xdelta_tool() -> str | None:
    return shutil.which("xdelta3") or shutil.which("xdelta")


def default_output(source: Path, patch: Path) -> Path:
    base = source.with_name(f"{source.stem} ({patch.stem}){source.suffix}")
    n = 2
    while base.exists():
        base = source.with_name(f"{source.stem} ({patch.stem}) ({n}){source.suffix}")
        n += 1
    return base


def apply_patch(source: Path, patch: Path, output: Path | None = None) -> tuple[Path, list[str]]:
    """Patch `source` with `patch`, writing a new file (never over the source). Returns (output path, notes)."""
    kind = detect(patch)
    if not kind:
        raise PatchError("Unknown patch format (expected IPS, UPS, BPS or xdelta)")
    out = output or default_output(source, patch)
    if out.exists():
        raise PatchError(f"{out.name} already exists")
    if kind == "xdelta":
        tool = xdelta_tool()
        if not tool:
            raise PatchError("xdelta patches need the xdelta3 program on PATH")
        done = subprocess.run([tool, "-d", "-s", str(source), str(patch), str(out)], capture_output=True, text=True,
                              creationflags=SUBPROCESS_FLAGS)
        if done.returncode:
            out.unlink(missing_ok=True)
            raise PatchError((done.stderr or "xdelta failed").strip().splitlines()[-1])
        return out, ["Applied by xdelta3 (it checks its own checksums when the patch has them)"]
    try:
        src, pat = source.read_bytes(), patch.read_bytes()
    except OSError as e:
        raise PatchError(str(e)) from e
    result, notes = {"ips": apply_ips, "bps": apply_bps, "ups": apply_ups}[kind](src, pat)
    out.write_bytes(result)
    return out, notes


def source_info(patch: Path) -> tuple[int, str] | None:
    """(source size, source CRC32 as hex) a UPS / BPS patch expects; None for IPS and xdelta, which state neither."""
    kind = detect(patch)
    if kind not in ("ups", "bps"):
        return None
    try:
        data = patch.read_bytes()
        r = _Reader(data[:-12], 4)
        return r.number(), f"{_footer(data)[0]:08x}"
    except (OSError, PatchError):
        return None


def output_name(source: Path, patches: list[Path]) -> Path:
    """'Game (patch).ext' or, for several patches applied in turn, 'Game (one + two).ext'; never an existing file."""
    base = source.with_name(f"{source.stem} ({' + '.join(p.stem for p in patches)}){source.suffix}")
    n = 2
    while base.exists():
        base = source.with_name(f"{source.stem} ({' + '.join(p.stem for p in patches)}) ({n}){source.suffix}")
        n += 1
    return base


def apply_chain(source: Path, patches: list[Path], output: Path) -> list[str]:
    """Apply `patches` one after another; only the final file is kept. Each step is verified on its own."""
    if output.exists():
        raise PatchError(f"{output.name} already exists")
    current, notes, temps = source, [], []
    try:
        for n, patch in enumerate(patches):
            last = n == len(patches) - 1
            step_out = output if last else output.with_name(f".distill_step{n}{output.suffix}")
            step_out.unlink(missing_ok=True)
            _out, step_notes = apply_patch(current, patch, step_out)
            notes.append(f"{patch.name}: " + "; ".join(step_notes))
            if not last:
                temps.append(step_out)
            current = step_out
    except Exception:
        output.unlink(missing_ok=True)
        raise
    finally:
        for t in temps:
            t.unlink(missing_ok=True)
    return notes
