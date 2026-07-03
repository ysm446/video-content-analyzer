import os
import threading

import torch

from .model_catalog import (
    available_translator_models as catalog_translator_models,
    default_translator_model_id,
    get_text_model_meta,
)
from .llama_server import LlamaServerManager
from .vram import max_memory_map
from .video_reviewer import _vision_server
from . import prompts

LLAMA_CPP_PORT = int(os.environ.get("LLAMA_CPP_PORT", "8766"))

# /no_think でthinkingモードをOFF → 字幕バッチ翻訳に最適化
SYSTEM_PROMPT = (
    "/no_think\n"
    "You are a professional subtitle translator. Translate the target line to natural, "
    "conversational Japanese suitable for on-screen subtitles.\n"
    "Rules:\n"
    "- Aim for concise subtitles (roughly 40 Japanese characters or fewer per line).\n"
    "- Use the surrounding context to resolve sentences that are split across lines, "
    "but translate ONLY the target line. Do not repeat content that belongs to other lines.\n"
    "- Keep proper nouns consistent throughout. If a glossary is provided, follow it. "
    "Do not force-translate names that are better left as-is.\n"
    "- Keep tone and terminology consistent with the previous translations shown as context.\n"
    "- Output only the Japanese translation of the target line, nothing else."
)

LOOKUP_SYSTEM_PROMPT = (
    "/no_think\n"
    "You are a bilingual dictionary assistant. When given an English word, respond in Japanese "
    "with this exact format (no extra text):\n"
    "【品詞】名詞／動詞／形容詞 など\n"
    "【意味】日本語の意味（簡潔に）\n"
    "【例文】An example sentence. ／ 日本語訳\n"
    "If the word has multiple common meanings, list up to 2."
)

# 保守的な ASR 補正。原文の言語・意味・語順を保ち、明らかな認識誤りだけを直す
REFINE_SYSTEM_PROMPT = (
    "/no_think\n"
    "You proofread raw speech-to-text (ASR) subtitle lines. Fix ONLY clear recognition "
    "errors: misrecognized or homophone words, obvious typos, and missing punctuation or "
    "capitalization. Keep the original language. Do NOT translate, summarize, paraphrase, "
    "reorder, add, or remove information, and do not merge or split lines. Preserve the "
    "wording and meaning as much as possible. If the line already looks correct, output it "
    "unchanged. Output only the corrected line, with no quotes, labels, or extra text."
)


def available_translator_models() -> list[dict]:
    return catalog_translator_models()


def get_prompts() -> list[dict]:
    """設定画面での閲覧用にこのモジュールのシステムプロンプトを返す。"""
    return [
        {"key": "translate", "label": "翻訳（字幕）", "category": "翻訳", "default": SYSTEM_PROMPT},
        {"key": "lookup", "label": "辞書検索", "category": "翻訳", "default": LOOKUP_SYSTEM_PROMPT},
        {"key": "refine", "label": "文字起こし補正", "category": "翻訳", "default": REFINE_SYSTEM_PROMPT},
    ]


_llama_cpp = LlamaServerManager(
    port=LLAMA_CPP_PORT,
    meta_resolver=get_text_model_meta,
    label="翻訳",
    startup_timeout=120.0,
)


class Translator:
    def __init__(self):
        self.model_id = default_translator_model_id() or ""
        self.model = None
        self.tokenizer = None
        self._lock = threading.RLock()

    @property
    def loaded(self) -> bool:
        if self._is_gguf_model():
            if self._uses_shared_vision_server():
                return _vision_server.loaded_for(self.model_id)
            return _llama_cpp.loaded_for(self.model_id)
        return self.model is not None and self.tokenizer is not None

    def _is_gguf_model(self) -> bool:
        return self.model_id.startswith("gguf:") and get_text_model_meta(self.model_id) is not None

    def _uses_shared_vision_server(self) -> bool:
        meta = get_text_model_meta(self.model_id)
        return bool(meta and meta.get("has_mmproj"))

    def load(self):
        with self._lock:
            if self._is_gguf_model():
                if self._uses_shared_vision_server():
                    _vision_server.acquire_model(self.model_id, "translator")
                    self.model = {"backend": "llama.cpp-vision-shared", "model_id": self.model_id}
                else:
                    _llama_cpp.ensure_model(self.model_id)
                    self.model = {"backend": "llama.cpp", "model_id": self.model_id}
                self.tokenizer = None
                return

            if self.model is not None and self.tokenizer is not None:
                return
            try:
                # HF フォールバック経路でのみ transformers を遅延 import（通常は GGUF 経路）
                from transformers import AutoModelForCausalLM, AutoTokenizer
                self.tokenizer = AutoTokenizer.from_pretrained(self.model_id)
                mm = max_memory_map()
                self.model = AutoModelForCausalLM.from_pretrained(
                    self.model_id,
                    torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
                    device_map="auto",
                    **({"max_memory": mm} if mm else {}),
                )
                print(f"[Translator] Loaded {self.model_id}")
            except Exception:
                self.model = None
                self.tokenizer = None
                raise

    def unload(self):
        with self._lock:
            self._unload()

    def _unload(self):
        if self._is_gguf_model():
            if self._uses_shared_vision_server():
                _vision_server.release_client("translator")
            else:
                _llama_cpp.stop()
            self.model = None
            self.tokenizer = None
            print("[Translator] llama.cpp モデルをアンロードしました")
            return

        if self.model is not None:
            del self.model
            del self.tokenizer
            self.model = None
            self.tokenizer = None
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            print("[Translator] モデルをアンロードしました")

    def set_model_id(self, model_id: str):
        with self._lock:
            if self.model_id != model_id:
                self._unload()
                self.model_id = model_id
                print(f"[Translator] モデルを {model_id} に変更（次回使用時にロード）")

    def _ensure_loaded(self):
        with self._lock:
            if not self.loaded:
                self.load()

    def _chat_llama_cpp(self, messages: list[dict], max_tokens: int) -> str:
        result, _meta = self._chat_llama_cpp_meta(messages, max_tokens)
        return result

    def _chat_llama_cpp_meta(self, messages: list[dict], max_tokens: int, response_format: dict | None = None) -> tuple[str, dict]:
        if self._uses_shared_vision_server():
            result, meta = _vision_server.chat_with_meta(self.model_id, messages, max_tokens, response_format)
        else:
            result, meta = _llama_cpp.chat_with_meta(self.model_id, messages, max_tokens, response_format)
        if "</think>" in result:
            result = result.split("</think>", 1)[-1].strip()
        return result, meta

    @staticmethod
    def _build_translate_user_message(
        text: str,
        context: list[tuple[str, str]] | None,
        lookahead: list[str] | None,
    ) -> str:
        """翻訳対象と参考文脈を1つの user メッセージに構造化する。

        以前は過去の (原文, 訳文) を user/assistant の擬似会話履歴として渡していたが、
        初期の誤訳を few-shot として模倣し続けるドリフトの原因になるため、
        「参考」であることを明示したラベル付きブロックに変更した。
        lookahead（続きの原文）は文の途中で切れたセグメントの誤訳対策。
        """
        parts: list[str] = []
        if context:
            ctx_lines = [f"Source: {orig}\nJapanese: {jp}" for orig, jp in context]
            parts.append("[Previous lines and their translations (reference only)]\n" + "\n".join(ctx_lines))
        if lookahead:
            parts.append("[Upcoming source lines (reference only, do NOT translate)]\n" + "\n".join(lookahead))
        parts.append("[Target line — translate ONLY this line]\n" + text)
        return "\n\n".join(parts)

    def translate(
        self,
        text: str,
        context: list[tuple[str, str]] | None = None,
        lookahead: list[str] | None = None,
        extra_system: str = "",
    ) -> str:
        result, _meta = self.translate_ex(text, context, lookahead, extra_system)
        return result

    def translate_ex(
        self,
        text: str,
        context: list[tuple[str, str]] | None = None,
        lookahead: list[str] | None = None,
        extra_system: str = "",
    ) -> tuple[str, dict]:
        """1行を翻訳して (訳文, 生成メタ) を返す。

        meta の finish_reason=="length" は max_tokens 打ち切り（訳が不完全）を意味する。
        extra_system には動画の文脈・用語集など呼び出し側が組み立てた追記を渡す。
        """
        with self._lock:
            self._ensure_loaded()
            system = prompts.resolve("translate", SYSTEM_PROMPT)
            if extra_system:
                system = f"{system}\n\n{extra_system}"
            messages: list[dict] = [
                {"role": "system", "content": system},
                {"role": "user", "content": self._build_translate_user_message(text, context, lookahead)},
            ]

            if self._is_gguf_model():
                return self._chat_llama_cpp_meta(messages, max_tokens=256)

            prompt = self.tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
            )
            inputs = self.tokenizer(prompt, return_tensors="pt").to(self.model.device)

            from transformers import GenerationConfig
            gen_config = GenerationConfig(do_sample=False, max_new_tokens=256)
            with torch.no_grad():
                output_ids = self.model.generate(
                    **inputs,
                    generation_config=gen_config,
                )

            generated_ids = output_ids[0][inputs.input_ids.shape[1]:]
            result = self.tokenizer.decode(generated_ids, skip_special_tokens=True).strip()

        if "</think>" in result:
            result = result.split("</think>", 1)[-1].strip()

        return result, {}

    def refine(self, text: str, context: list[tuple[str, str]] | None = None) -> str:
        """ASR 字幕行を保守的に補正する。原文の言語・意味を保ち誤認識だけ直す。"""
        with self._lock:
            self._ensure_loaded()
            messages: list[dict] = [{"role": "system", "content": prompts.resolve("refine", REFINE_SYSTEM_PROMPT)}]

            if context:
                for orig, fixed in context:
                    messages.append({"role": "user", "content": f"Correct ASR errors:\n{orig}"})
                    messages.append({"role": "assistant", "content": fixed})

            messages.append({"role": "user", "content": f"Correct ASR errors:\n{text}"})

            if self._is_gguf_model():
                return self._chat_llama_cpp(messages, max_tokens=512)

            prompt = self.tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
            )
            inputs = self.tokenizer(prompt, return_tensors="pt").to(self.model.device)

            from transformers import GenerationConfig
            gen_config = GenerationConfig(do_sample=False, max_new_tokens=512)
            with torch.no_grad():
                output_ids = self.model.generate(
                    **inputs,
                    generation_config=gen_config,
                )

            generated_ids = output_ids[0][inputs.input_ids.shape[1]:]
            result = self.tokenizer.decode(generated_ids, skip_special_tokens=True).strip()

        if "</think>" in result:
            result = result.split("</think>", 1)[-1].strip()

        return result

    def lookup(self, word: str) -> str:
        with self._lock:
            self._ensure_loaded()
            messages = [
                {"role": "system", "content": prompts.resolve("lookup", LOOKUP_SYSTEM_PROMPT)},
                {"role": "user", "content": word.strip()},
            ]

            # 品詞・意味・例文（最大2義）を出し切れるよう 256 を確保（128 だと例文が途中で切れる）
            if self._is_gguf_model():
                return self._chat_llama_cpp(messages, max_tokens=256)

            prompt = self.tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
            )
            inputs = self.tokenizer(prompt, return_tensors="pt").to(self.model.device)

            from transformers import GenerationConfig
            gen_config = GenerationConfig(do_sample=False, max_new_tokens=256)
            with torch.no_grad():
                output_ids = self.model.generate(
                    **inputs,
                    generation_config=gen_config,
                )

            generated_ids = output_ids[0][inputs.input_ids.shape[1]:]
            result = self.tokenizer.decode(generated_ids, skip_special_tokens=True).strip()

            if "</think>" in result:
                result = result.split("</think>", 1)[-1].strip()

            return result
