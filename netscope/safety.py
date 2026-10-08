"""Commands that must never be pushed from the UI or from an AI-generated plan."""
import re

BLOCKED = re.compile(r"^\s*(reload|erase|write\s+erase|delete|format|no\s+username|username|crypto\s+key\s+zeroize|"
                     r"boot\s+system|copy\s+\S+\s+(startup|flash|tftp|ftp)|enable\s+(secret|password)|"
                     r"line\s+(vty|con))\b", re.I)
EXEC_OK = re.compile(r"^\s*(show|ping|traceroute)\b", re.I)
