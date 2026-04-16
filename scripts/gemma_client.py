import os
import requests


GEMMA_ENDPOINT = os.getenv("GEMMA_ENDPOINT", "http://100.100.108.44:8013/v1/chat/completions")
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "google/gemma-4-26B-A4B-it")
GEMMA_API_KEY = os.getenv("GEMMA_API_KEY", "").strip()


def gemma_translate_markdown(prompt_text: str, markdown_text: str,
                             max_tokens: int = 8192,
                             temperature: float = 0.0,
                             top_p: float = 0.95,
                             timeout: int = 300) -> str:
    headers = {
        "Content-Type": "application/json",
    }
    if GEMMA_API_KEY:
        headers["Authorization"] = f"Bearer {GEMMA_API_KEY}"

    user_content = f"{prompt_text}\n\n--- BEGIN MARKDOWN ---\n{markdown_text}\n--- END MARKDOWN ---"

    payload = {
        "model": GEMMA_MODEL,
        "messages": [
            {"role": "system", "content": "You are a precise technical translator."},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
    }

    response = requests.post(GEMMA_ENDPOINT, headers=headers, json=payload, timeout=timeout)
    response.raise_for_status()
    data = response.json()

    try:
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        raise RuntimeError(f"Unexpected response format: {data}") from e
