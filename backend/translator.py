import torch
import threading
from transformers import AutoTokenizer, AutoModelForCausalLM, GenerationConfig

from .vram import max_memory_map

MODEL_ID = "Qwen/Qwen3-1.7B"

# /no_think でthinkingモードをOFF → 字幕バッチ翻訳に最適化
SYSTEM_PROMPT = (
    "/no_think\n"
    "You are a subtitle translator. Translate the given text to natural Japanese "
    "suitable for subtitle display. Output only the Japanese translation, nothing else."
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


class Translator:
    def __init__(self):
        self.model_id  = MODEL_ID  # 実行時に変更可能
        self.model     = None
        self.tokenizer = None
        self._lock     = threading.RLock()

    def load(self):
        with self._lock:
            if self.model is not None and self.tokenizer is not None:
                return
            try:
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
                # 部分的に初期化された状態を残さない
                self.model = None
                self.tokenizer = None
                raise

    def unload(self):
        """モデルを破棄して VRAM を解放する（公開API）"""
        with self._lock:
            self._unload()

    def _unload(self):
        """モデルを破棄して VRAM を解放する"""
        if self.model is not None:
            del self.model
            del self.tokenizer
            self.model     = None
            self.tokenizer = None
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            print("[Translator] モデルをアンロードしました")

    def set_model_id(self, model_id: str):
        """翻訳モデルを切り替える。ロード済みの場合は即アンロードして次回使用時にロード。"""
        with self._lock:
            if self.model_id != model_id:
                self._unload()
                self.model_id = model_id
                print(f"[Translator] モデルを {model_id} に変更（次回使用時にロード）")

    def _ensure_loaded(self):
        """モデルが未ロードであればオンデマンドでロードする"""
        with self._lock:
            if self.model is None or self.tokenizer is None:
                self.load()

    def translate(self, text: str, context: list[tuple[str, str]] | None = None) -> str:
        """テキスト1件を日本語に翻訳して返す。

        Args:
            text:    翻訳対象のテキスト
            context: 直前セグメントの (原文, 翻訳) ペアのリスト。
                     チャット履歴として渡すことで代名詞・用語の一貫性が向上する。
        """
        with self._lock:
            self._ensure_loaded()
            messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

            # 直前セグメントをチャット履歴として追加（コンテキスト窓）
            if context:
                for orig, jp in context:
                    messages.append({"role": "user",      "content": f"Translate to Japanese:\n{orig}"})
                    messages.append({"role": "assistant",  "content": jp})

            messages.append({"role": "user", "content": f"Translate to Japanese:\n{text}"})
            prompt = self.tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
            )
            inputs = self.tokenizer(prompt, return_tensors="pt").to(self.model.device)

            # GenerationConfig を明示的に渡してモデルのデフォルト設定（temperature等）を上書き
            gen_config = GenerationConfig(do_sample=False, max_new_tokens=256)
            with torch.no_grad():
                output_ids = self.model.generate(
                    **inputs,
                    generation_config=gen_config,
                )

            generated_ids = output_ids[0][inputs.input_ids.shape[1]:]
            result = self.tokenizer.decode(generated_ids, skip_special_tokens=True).strip()

        # thinkingトークンが残っている場合は除去
        if "</think>" in result:
            result = result.split("</think>", 1)[-1].strip()

            return result

    def lookup(self, word: str) -> str:
        """英単語の日本語定義を生成して返す"""
        with self._lock:
            self._ensure_loaded()
            messages = [
                {"role": "system", "content": LOOKUP_SYSTEM_PROMPT},
                {"role": "user", "content": word.strip()},
            ]
            prompt = self.tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
            )
            inputs = self.tokenizer(prompt, return_tensors="pt").to(self.model.device)

            gen_config = GenerationConfig(do_sample=False, max_new_tokens=128)
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
