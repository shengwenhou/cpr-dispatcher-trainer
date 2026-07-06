"""測試輔助：快速建構 IntentResult，與從動作清單抽取 line id。"""
from __future__ import annotations

from server.engine.actions import SpeakAction, SpeakKind
from server.engine.intents import IntentResult, Slot, SlotValue


def intent(*slots, confidence: float = 0.9, faq_id=None, end_signal=False, counting=False) -> IntentResult:
    """以 (Slot, SlotValue) 對建構 IntentResult。"""
    res = IntentResult(source="test", confidence=confidence, faq_id=faq_id, end_signal=end_signal, counting=counting)
    for s, v in slots:
        res.slots[s] = v
    return res


def ids(actions: list[SpeakAction]) -> list[str]:
    """抽出動作清單裡的預錄 line id（動態句以 <dynamic> 標記）。"""
    out = []
    for a in actions:
        if a.kind == SpeakKind.PRERECORDED and a.line_id:
            out.append(a.line_id)
        elif a.kind == SpeakKind.FILLER_THEN_DYNAMIC:
            if a.filler_id:
                out.append(a.filler_id)
            out.append("<dynamic>")
        elif a.kind == SpeakKind.DYNAMIC:
            out.append("<dynamic>")
    return out


# 常用 slot 值捷徑
AMBULANCE = (Slot.WANTS_AMBULANCE, SlotValue.YES)
FIRE = (Slot.WANTS_AMBULANCE, SlotValue.NO)
LOCATION = (Slot.LOCATION, SlotValue.PROVIDED)
NO_CONSCIOUS = (Slot.CONSCIOUSNESS, SlotValue.NO)
NO_BREATH = (Slot.BREATHING, SlotValue.ABSENT)
AGONAL = (Slot.BREATHING, SlotValue.AGONAL)
UNCLEAR_BREATH = (Slot.BREATHING, SlotValue.UNCLEAR)
POSITIONED = (Slot.POSITIONING_DONE, SlotValue.YES)
COMPRESSING = (Slot.COMPRESSIONS_STARTED, SlotValue.YES)
