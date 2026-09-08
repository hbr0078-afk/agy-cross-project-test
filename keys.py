import os

def get_api_key_by_index(index: int = 1) -> str:
    """
    Retrieve API key for given 1-based index with priority:
    1. AGY_KEY_{index}
    2. GEMINI_API_KEY_{index} (or GEMINI_API_KEY for index 1)
    Also reads from ~/.bashrc if not present in os.environ.
    Never logs or exposes the key value.
    """
    primary_name = f"AGY_KEY_{index}"
    fallback_name = f"GEMINI_API_KEY_{index}" if index > 1 else "GEMINI_API_KEY"

    # 1. Check environment variables
    if primary_name in os.environ and os.environ[primary_name].strip():
        return os.environ[primary_name].strip()
    if fallback_name in os.environ and os.environ[fallback_name].strip():
        return os.environ[fallback_name].strip()

    # 2. Check ~/.bashrc directly (for daemon/subprocess sessions)
    bashrc_path = os.path.expanduser("~/.bashrc")
    primary_val = ""
    fallback_val = ""
    if os.path.exists(bashrc_path):
        with open(bashrc_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"export {primary_name}="):
                    val = line.split("=", 1)[1].strip("\"' ")
                    if val:
                        primary_val = val
                elif line.startswith(f"export {fallback_name}="):
                    val = line.split("=", 1)[1].strip("\"' ")
                    if val:
                        fallback_val = val

    if primary_val:
        return primary_val
    if fallback_val:
        return fallback_val

    return ""
