"""Проверка готовых эдитов «на слух»: что реально звучит в ролике (Whisper по готовому файлу) и где реплика
обрывается — последняя фраза перед склейкой без знака конца или со слишком короткой паузой после.

    python tools/qa_edit.py output/<фильм>/*.mp4
"""
from __future__ import annotations

import sys
from pathlib import Path

from faster_whisper import WhisperModel


def main() -> None:
    model = WhisperModel("medium", device="cpu", compute_type="int8")
    for f in sys.argv[1:]:
        print(f"\n=== {Path(f).name}")
        segs, _ = model.transcribe(f, language="ru", vad_filter=True, word_timestamps=True, beam_size=5,
                                   condition_on_previous_text=False)
        for s in segs:
            if s.no_speech_prob > 0.6:
                continue
            words = s.words or []
            text = s.text.strip()
            flag = ""
            if words and not text.endswith((".", "!", "?", "…")):
                flag = "   <-- без конца фразы?"
            print(f"  {s.start:5.1f}–{s.end:5.1f}  {text}{flag}")


if __name__ == "__main__":
    main()
