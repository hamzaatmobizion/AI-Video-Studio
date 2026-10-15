"""python -m app.doctor: report web-app configuration problems."""
import sys

from app.config import get_settings


def main() -> int:
    probs = get_settings().problems()
    for p in probs:
        print(f"✗ {p}")
    if not probs:
        print("✓ Web app configuration looks good.")
    return 1 if get_settings().fatal_problems() else 0


if __name__ == "__main__":
    sys.exit(main())
