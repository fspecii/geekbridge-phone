# SPDX-License-Identifier: GPL-3.0-or-later
"""Copies one device's pairing key from macOS into our private key file. Touches no Bluetooth.

    python -I keys_import.py <address or part of the device's name> [--reversed]

macOS keeps classic pairing keys in the System keychain (service "MobileBluetooth", account =
the device address). Reading one makes macOS ask for an administrator's approval; that prompt
is the user's consent. The key is written to the same private file the phone's key lives in
(mode 600, outside the repository) and is never printed: this tool only reports what shape
the keychain item had.

`--reversed` stores the 16 bytes in the opposite order. Stacks disagree on the byte order of a
link key at rest; if the device refuses authentication with one order, import again with the other.
"""
import argparse
import binascii
import plistlib
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ADDRESS = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
KEYCHAIN = "/Library/Keychains/System.keychain"
SERVICE = "MobileBluetooth"
KEY_NAMES = ("linkkey", "link_key", "key")
TYPE_NAMES = ("linkkeytype", "keytype", "type")
DEFAULT_TYPE = 4  # unauthenticated combination key: what a mouse or headphones normally have


class ImportError_(Exception):
    pass


def _walk(node, name=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk(value, str(key))
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _walk(value, name)
    else:
        yield name, node


def _key_bytes(value):
    """macOS 27 writes the key as text, sixteen hex pairs joined by dashes; older shapes use raw bytes."""
    if isinstance(value, str) and re.fullmatch(r"[0-9A-Fa-f]{2}([-:][0-9A-Fa-f]{2}){15}", value.strip()):
        return bytes.fromhex(re.sub(r"[-:]", "", value.strip()))
    return value


def _key_type(value):
    """The key type as a number. macOS 27 writes a word; the P-192 numbers stand in for it, since
    the number only tells our store whether the key was authenticated."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return {"authenticated": 5, "unauthenticated": 4}.get(value.strip().lower())
    return None


def parse_secret(secret):
    """Returns (key bytes, key type, shape). `shape` describes the item without revealing it."""
    data = secret.strip() if isinstance(secret, bytes) else secret.strip().encode()
    shape = []
    if re.fullmatch(rb"[0-9a-fA-F]+", data) and len(data) % 2 == 0 and len(data) != 16:
        data = binascii.unhexlify(data)
        shape.append("hex")
    if len(data) == 16 and not data.startswith((b"bplist", b"<?xml")):
        return data, DEFAULT_TYPE, "+".join(shape + ["raw-16"])
    if data.startswith((b"bplist", b"<?xml", b"<plist")):
        try:
            tree = plistlib.loads(data)
        except Exception:
            raise ImportError_("the keychain item looks like a property list but does not parse") from None
        shape.append("plist")
        leaves = list(_walk(tree))
        names = sorted({name for name, _ in leaves})
        leaves = [(name, _key_bytes(value)) for name, value in leaves]
        candidates = [(name, value) for name, value in leaves if isinstance(value, bytes) and len(value) == 16]
        named = [value for name, value in candidates if name.lower().replace(" ", "") in KEY_NAMES]
        chosen = named or [value for _, value in candidates]
        if len(chosen) != 1:
            raise ImportError_(f"expected one 16-byte key in the item, found {len(chosen)}; fields: {names}")
        kind = next((_key_type(value) for name, value in leaves
                     if name.lower().replace(" ", "") in TYPE_NAMES and _key_type(value) is not None), DEFAULT_TYPE)
        return chosen[0], kind, "+".join(shape) + f" fields={names}"
    raise ImportError_(f"unrecognised keychain item: {len(data)} bytes")


def read_keychain(address):
    result = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", SERVICE, "-a", address, "-w",
                             KEYCHAIN], capture_output=True)
    if result.returncode != 0:
        raise ImportError_("macOS has no pairing key for that address, or the request was not approved")
    return result.stdout


def store(controller, address, key, kind, path=None):
    from bumble import hci
    from gbp_acipc import TargetKeyStore
    # Named as Bumble names them, so the stack finds the entry by its own lookup.
    keystore = TargetKeyStore(hci.Address(controller, hci.Address.PUBLIC_DEVICE_ADDRESS),
                              hci.Address(address, hci.Address.PUBLIC_DEVICE_ADDRESS), path)
    entries = keystore._entries()
    entries[keystore.account] = {"type": int(kind), "key": bytes(key).hex(), "source": "macos"}
    keystore._save(entries)
    return keystore.path


def controller_address():
    """This Mac's Bluetooth address, as system_profiler reports it."""
    text = subprocess.run(["/usr/sbin/system_profiler", "SPBluetoothDataType"], capture_output=True, text=True).stdout
    match = re.search(r"Address:\s*(([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})", text)
    if not match:
        raise ImportError_("could not read this Mac's Bluetooth address")
    return match.group(1).upper()


def find_device(text, listing):
    """The address of the one paired device whose name contains `text`, from system_profiler's listing."""
    found, name = {}, None
    for line in listing.splitlines():
        heading = re.match(r"^\s{10}(\S.*):\s*$", line)
        if heading:
            name = heading.group(1)
        address = re.search(r"Address:\s*(([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})", line)
        if address and name and text.lower() in name.lower():
            found[address.group(1).upper()] = name
    if len(found) != 1:
        names = sorted(found.values())
        raise ImportError_(f"{len(found)} paired devices match {text!r}" + (f": {names}" if names else ""))
    return next(iter(found))


def resolve(text):
    """An address as given, or the address of the paired device named like `text`."""
    if ADDRESS.match(text.upper()):
        return text.upper()
    listing = subprocess.run(["/usr/sbin/system_profiler", "SPBluetoothDataType"], capture_output=True, text=True)
    return find_device(text, listing.stdout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("device", help="a Bluetooth address, or part of a paired device's name")
    parser.add_argument("--reversed", action="store_true")
    parser.add_argument("--print-address", action="store_true", help="only print the address and stop")
    args = parser.parse_args()
    try:
        address = resolve(args.device)
    except ImportError_ as error:
        sys.exit(f"not imported: {error}")
    if args.print_address:
        print(address)
        return
    try:
        key, kind, shape = parse_secret(read_keychain(address))
        if args.reversed:
            key = key[::-1]
        path = store(controller_address(), address, key, kind)
    except ImportError_ as error:
        sys.exit(f"not imported: {error}")
    print(f"imported: key type {kind}, item shape {shape}, byte order {'reversed' if args.reversed else 'as stored'}")
    print(f"saved to {path} (mode 600); the key itself is not shown")


if __name__ == "__main__":
    main()
