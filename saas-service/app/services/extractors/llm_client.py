"""
LLM client utilities for making LLM calls.
"""

from typing import Tuple, Any
import os
import random
import time


def make_client(cfg: dict) -> Tuple[str, Any, dict]:
    """根据 config.llm.provider 选择 LLM 后端
    返回 (provider_type, client, provider_config)
    """
    provider = cfg["llm"]["provider"]
    p_cfg = cfg["llm"]["providers"].get(provider)
    if not p_cfg:
        raise RuntimeError(
            f"未配置 provider: {provider}（合法值: {list(cfg['llm']['providers'].keys())}）"
        )

    api_key = os.getenv(p_cfg["api_key_env"], "").strip()
    if not api_key:
        raise RuntimeError(f"环境变量 {p_cfg['api_key_env']} 未设置")

    if provider == "anthropic":
        try:
            from anthropic import Anthropic
        except ImportError:
            raise RuntimeError("anthropic SDK 未装：pip install anthropic")
        return ("anthropic", Anthropic(api_key=api_key), p_cfg)
    else:
        try:
            from openai import OpenAI
        except ImportError:
            raise RuntimeError("openai SDK 未装：pip install openai")
        return ("openai_compat",
                OpenAI(api_key=api_key, base_url=p_cfg["base_url"]),
                p_cfg)


def call_llm(provider_type: str, client, p_cfg: dict,
             prompt: str, max_tokens: int,
             max_retries: int = 5) -> Tuple[str, int, int]:
    """单次调用 + 指数退避。
    返回 (text_response, input_tokens, output_tokens)。
    遇到 429 / rate limit 时按 1s, 2s, 4s, 8s, 16s 退避。
    """
    last_err = None
    for attempt in range(max_retries):
        try:
            if provider_type == "anthropic":
                resp = client.messages.create(
                    model=p_cfg["model"],
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                )
                return (
                    resp.content[0].text.strip(),
                    resp.usage.input_tokens,
                    resp.usage.output_tokens,
                )
            else:
                resp = client.chat.completions.create(
                    model=p_cfg["model"],
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                    stream=False,
                )
                content = resp.choices[0].message.content or ""
                usage = resp.usage
                in_tok = (usage.prompt_tokens if usage else 0) or 0
                out_tok = (usage.completion_tokens if usage else 0) or 0
                return (content.strip(), in_tok, out_tok)
        except Exception as e:
            last_err = e
            err_str = str(e).lower()
            is_rate_limit = (
                "429" in err_str or
                "rate" in err_str or
                "rpm" in err_str or
                "quota" in err_str or
                "too many" in err_str
            )
            if not is_rate_limit:
                raise
            if attempt == max_retries - 1:
                break
            wait = (2 ** attempt) + random.uniform(0, 1)
            time.sleep(wait)
    raise last_err
