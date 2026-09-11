"""
dashboard_2fa_setup.py
Provision Google Authenticator (TOTP) for the web dashboard's Settings tab.

RUN THIS ON THE SERVER, over SSH — never expose it as an HTTP endpoint. A
publicly reachable "generate me a new secret" route would let anyone re-provision
two-factor and hand themselves write access, which defeats the point.

    python dashboard_2fa_setup.py            # provision (refuses to clobber)
    python dashboard_2fa_setup.py --force    # rotate an existing secret
    python dashboard_2fa_setup.py --check    # verify a code, change nothing

Writes DASHBOARD_TOTP_SECRET into .env, then restart the dashboard so the new
value is read. Until that key exists the Settings tab is read-only.
"""
import os
import io
import sys
import argparse
import tempfile

# This file lives in the PROJECT ROOT, beside .env — not in tools/.
# The Ubuntu deployment carries only the live project, so a tools/
# subdirectory would not exist there. One dirname, not two: two would
# resolve to the parent of the project and write a stray .env there.
_ROOT = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(_ROOT, ".env")
KEY = "DASHBOARD_TOTP_SECRET"
ISSUER = "CSB Trading Bot"

try:
    import pyotp
except ImportError:
    sys.exit("pyotp is not installed.  pip install pyotp")

# Optional. Without it the setup key is still printed for manual entry, so a
# missing package degrades the experience rather than blocking provisioning.
try:
    import qrcode
except ImportError:
    qrcode = None


def print_qr(uri: str) -> bool:
    """Render the provisioning URI as a scannable QR in the terminal.

    invert=True because a scanner needs DARK modules on a LIGHT background. On
    a dark terminal the naive rendering comes out photo-negative and phones
    silently fail to read it.
    """
    if qrcode is None:
        return False
    try:
        qr = qrcode.QRCode(border=2)
        qr.add_data(uri)
        qr.make(fit=True)
        buf = io.StringIO()
        qr.print_ascii(out=buf, invert=True)
        print(buf.getvalue())
        return True
    except Exception:
        return False


def read_env() -> dict:
    out = {}
    if not os.path.exists(ENV_FILE):
        return out
    with open(ENV_FILE, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def write_key(secret: str) -> None:
    """Insert or replace KEY, preserving every other line byte-for-byte.

    Atomic: written to a temp file alongside .env and then replaced, so an
    interrupted run cannot leave the file truncated — .env holds the API
    credentials and a half-written one is a genuine outage.
    """
    lines = []
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines(keepends=True)

    nl = "\r\n" if (lines and lines[0].endswith("\r\n")) else "\n"
    replaced = False
    out = []
    for line in lines:
        stripped = line.strip()
        if (stripped and not stripped.startswith("#") and "=" in stripped
                and stripped.split("=", 1)[0].strip() == KEY):
            out.append(f"{KEY}={secret}{nl}")
            replaced = True
        else:
            out.append(line)

    if not replaced:
        if out and not out[-1].endswith("\n"):
            out.append(nl)
        out.append(nl)
        out.append(f"# Two-factor secret for the dashboard Settings tab.{nl}")
        out.append(f"# Provisioned by dashboard_2fa_setup.py. Treat this like{nl}")
        out.append(f"# a password: anyone holding it can change trading settings.{nl}")
        out.append(f"{KEY}={secret}{nl}")

    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(ENV_FILE) or ".",
                               prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.writelines(out)
        os.replace(tmp, ENV_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def main() -> None:
    ap = argparse.ArgumentParser(description="Provision dashboard 2FA")
    ap.add_argument("--force", action="store_true",
                    help="replace an existing secret (invalidates the old one)")
    ap.add_argument("--check", action="store_true",
                    help="verify a code against the stored secret, change nothing")
    ap.add_argument("--show-uri", action="store_true",
                    help="also print the otpauth:// URI (contains the secret "
                         "in plain text — avoid on shared screens)")
    args = ap.parse_args()

    env = read_env()
    existing = env.get(KEY, "").strip()

    if args.check:
        if not existing:
            sys.exit(f"No {KEY} in .env — nothing to check.")
        code = input("Enter the 6-digit code from Google Authenticator: ").strip()
        ok = pyotp.TOTP(existing).verify(code.replace(" ", ""), valid_window=1)
        print("\n  Code is VALID.\n" if ok else
              "\n  Code is NOT valid. Check the phone's clock is set to "
              "automatic — TOTP is time-based.\n")
        return

    if existing and not args.force:
        sys.exit(
            f"{KEY} already exists in .env.\n"
            "Re-run with --force to rotate it. Rotating invalidates the current\n"
            "authenticator entry, and you will need to scan the new one."
        )

    secret = pyotp.random_base32()
    uri = pyotp.TOTP(secret).provisioning_uri(name="dashboard", issuer_name=ISSUER)

    print()
    print("=" * 68)
    print("  DASHBOARD TWO-FACTOR SETUP")
    print("=" * 68)
    shown = print_qr(uri)
    if shown:
        print("  Scan the QR above with Google Authenticator "
              "(+ -> Scan a QR code).")
        print()
        print("  If the terminal squashes it, widen the window or type the key.")
    else:
        print("  Install qrcode for a scannable QR here:  pip install qrcode")
    print()
    print("  Manual entry: + -> Enter a setup key")
    print()
    print(f"    Account : {ISSUER} (dashboard)")
    print(f"    Key     : {secret}")
    print("    Type    : Time based")
    print()
    print("  Treat the QR and the key like a password. Anyone who photographs")
    print("  or screenshots either can generate valid codes indefinitely — if")
    print("  that happens, re-run with --force to rotate.")
    print()
    print("=" * 68)

    if args.show_uri:
        print()
        print("  Provisioning URI (contains the secret in plain text):")
        print(f"    {uri}")
    confirm = input("\n  Scan it now, then enter the current 6-digit code to "
                    "confirm: ").strip().replace(" ", "")
    if not pyotp.TOTP(secret).verify(confirm, valid_window=1):
        sys.exit("\n  That code did not match — nothing was written. "
                 "Re-run to try again.")

    write_key(secret)
    print(f"\n  Verified. {KEY} written to .env")
    print("  Restart the dashboard for it to take effect:")
    print("      sudo systemctl restart csb-web    (or however it is run)")
    print()
    print("  The Settings tab will now accept changes when you enter a code.")
    print("  Anyone WITHOUT a code still has read-only access.\n")


if __name__ == "__main__":
    main()
