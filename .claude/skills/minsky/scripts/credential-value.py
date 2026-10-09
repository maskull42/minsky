#!/usr/bin/env python3
"""Emit one allow-listed credential to the wrapper's captured substitution, never source shell code.

Do not invoke interactively on real credentials. Tests use synthetic dotenv files only.
All nonsecret settings are ignored; interpolation and shell evaluation are disabled.
"""
import argparse
import os
from dotenv import dotenv_values


def credential_value(path: str, name: str) -> str:
    aliases = {"GOOGLE_GENERATIVE_AI_API_KEY": ("GOOGLE_GENERATIVE_AI_API_KEY", "GOOGLE_API_KEY"),
               "deepseek_api": ("deepseek_api",)}
    if name not in aliases:
        raise ValueError("credential name is not authorised")
    values = dotenv_values(path, interpolate=False)
    for source in (os.environ, values):
        for key in aliases[name]:
            value = source.get(key)
            if value:
                if any(c in value for c in ("\n", "\r", "\x00")):
                    raise ValueError("credential contains forbidden control characters")
                return value
    raise ValueError("required provider credential is unavailable")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--credential", required=True)
    args = parser.parse_args()
    try:
        print(credential_value(args.env_file, args.credential), end="")
    except ValueError as exc:
        parser.exit(2, f"credential-value: {exc}\n")
