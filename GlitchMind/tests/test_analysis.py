import pytest

from glitchmind.analysis import patterns as pt
from glitchmind.analysis.deception import DeceptionTracker, extract_numbers, index_label
from glitchmind.analysis.engine import Engine, parse_dialog
from glitchmind.knowledge import kb


def ids(text, groups=None):
    return {h["id"] for h in pt.find_hits(text, groups)}


@pytest.mark.parametrize("text,expected", [
    ("Как ты мог так со мной поступить! После всего, что я для тебя сделала!", "guilt"),
    ("Если ты сейчас не согласишься, то можешь больше на меня не рассчитывать.", "fear_threat"),
    ("Решай прямо сейчас, предложение действует только до вечера", "pressure"),
    ("Нормальные люди так не поступают", "obligation"),
    ("Ты же умный человек, только ты можешь мне помочь", "flattery"),
    ("Тебе показалось, такого не было", "reality_denial"),
    ("Если ты уйдёшь, я с собой что-нибудь сделаю", "self_punish"),
    ("Никто меня не понимает, я всё тащу на себе", "sufferer"),
    ("Все так считают, и коллеги тоже против", "social_proof"),
    ("Ты идиот, заткнись", "attack"),
])
def test_manipulation_patterns_detected(text, expected):
    assert expected in ids(text)


@pytest.mark.parametrize("text", [
    "Да ладно, всё нормально, пойдём пить чай.",
    "Привет, как дела? Давай встретимся завтра в шесть.",
    "Если ты опять опоздаешь, купи хлеб.",  # не должно быть «критики»
])
def test_calm_text_has_no_manipulation(text):
    bad = {"guilt", "fear_threat", "self_punish", "reality_denial", "attack", "criticism", "obligation"}
    assert not (ids(text) & bad)


def test_emotions_detected():
    assert {"emo_fear", "emo_hurt"} <= ids("Мне так обидно, и мне страшно.")


def test_safety_flag():
    assert pt.safety_flag("Я не хочу больше жить")
    assert pt.safety_flag("я с собой что-нибудь сделаю")
    assert not pt.safety_flag("Я не хочу больше работать здесь")


def test_every_pattern_compiles_and_has_book():
    for p in pt.PATTERNS:
        assert p.regexes, p.id
        assert p.book == "" or kb.book(p.book), p.id


def test_numbers_extraction():
    assert extract_numbers("в шесть часов") == {"6"}
    assert extract_numbers("в двадцать пять минут 7") == {"25", "7"}
    assert extract_numbers("в 09:30") == {"9:30"}  # ведущие нули не важны
    assert extract_numbers("в 00:30 и 007") == {"0:30", "7"}


def test_numeric_contradiction():
    t = DeceptionTracker()
    t.process({"id": 1, "text": "Я вернулся домой в шесть часов вечера", "features": {}})
    cues, idx = t.process({"id": 2, "text": "Я вернулся домой в девять часов вечера", "features": {}})
    assert "contradiction_num" in {c.id for c in cues}
    assert idx > 0.3


def test_polarity_contradiction():
    t = DeceptionTracker()
    t.process({"id": 1, "text": "Я не брал твой телефон", "features": {}})
    cues, _ = t.process({"id": 2, "text": "Я брал твой телефон на минуту", "features": {}})
    assert "contradiction_pol" in {c.id for c in cues}


def test_no_contradiction_for_different_topics():
    t = DeceptionTracker()
    t.process({"id": 1, "text": "Вчера я купил хлеб в магазине", "features": {}})
    cues, _ = t.process({"id": 2, "text": "Сегодня погода хорошая и тёплая", "features": {}})
    assert not [c for c in cues if c.kind == "logic"]


def test_text_cues_and_disclaimer_flow():
    t = DeceptionTracker()
    cues, idx = t.process({"id": 1, "text": "Честно говоря, клянусь, я ничего не помню, кажется, вроде бы так", "features": {}})
    got = {c.id for c in cues}
    assert {"truth_protest", "memory_evasion"} <= got
    assert 0 < idx < 1


def test_voice_deviation_requires_baseline():
    t = DeceptionTracker()
    normal = {"wps": 3.0, "f0_mean": 200.0, "f0_std": 20.0, "latency": 1.0}
    # без базовой линии отклонения голоса не засчитываются
    cues, _ = t.process({"id": 0, "text": "Я был дома весь вечер, правда", "features": {"wps": 6.0, "f0_mean": 300.0}})
    assert not [c for c in cues if c.id in ("rate", "pitch")]
    for i in range(1, 6):
        t.process({"id": i, "text": f"Обычная фраза номер {i} для базовой линии", "features": dict(normal)})
    cues, _ = t.process({"id": 9, "text": "Я был дома весь вечер, правда", "features": {"wps": 5.2, "f0_mean": 250.0, "f0_std": 22.0}})
    got = {c.id for c in cues}
    assert "rate" in got and "pitch" in got


def test_index_decays_and_resets():
    t = DeceptionTracker()
    t.process({"id": 1, "text": "Клянусь, честно говоря, не помню, кажется", "features": {}})
    high = t.index
    for i in range(2, 8):
        t.process({"id": i, "text": "Хорошо, давай встретимся завтра днём у метро", "features": {}})
    assert t.index < high
    t.reset()
    assert t.index == 0
    assert index_label(0.0) == "ровно" and index_label(0.9).startswith("много")


# ------------------------------------------------------------------- движок
def run(dialog: str):
    e = Engine()
    items = parse_dialog(dialog)
    for u in items:
        u.update(e.analyze_utterance(u))
    return e, items


def test_parse_dialog_speakers():
    items = parse_dialog("Он: привет\nЯ: здравствуй\nпросто строка\n\n  \nСобеседник — как дела")
    assert [i["speaker"] for i in items] == ["them", "me", "them", "them"]
    assert items[1]["text"] == "здравствуй"


def test_advice_for_threat_has_four_styles_and_books():
    e, items = run("Он: Если ты опять опоздаешь, то я больше не буду с тобой разговаривать. Решай прямо сейчас.")
    a = e.build_advice(items)
    assert a["source"] == "rules"
    assert len(a["replies"]) >= 3
    assert {r["style"] for r in a["replies"]} >= {"Мягко", "Твёрдо"}
    assert all(kb.book(r["book"]) for r in a["replies"])
    assert a["questions"] and a["avoid"] and a["their_goal"]
    assert a["meters"]["pressure"] > 40


def test_advice_for_calm_text_is_general_with_mirror():
    e, items = run("Он: Мне нужно подумать над твоим предложением до пятницы")
    a = e.build_advice(items)
    assert "Явных манипулятивных" in a["situation"]
    assert any("до пятницы" in r["text"] for r in a["replies"])  # зеркало Восса


def test_empty_dialog_advice_is_safe():
    a = Engine().build_advice([])
    assert a["replies"] == [] and "нет реплик" in a["situation"].lower()


def test_coaching_for_my_speech():
    e, items = run("Он: Ты всегда всё портишь\nЯ: Извини, я просто, как бы, наверное, не хотел")
    a = e.build_advice(items)
    assert a["self_coaching"]
    assert e.meters(items)["manip"] >= 0


def test_safety_banner_in_advice():
    e, items = run("Она: Если ты уйдёшь, я с собой что-нибудь сделаю")
    a = e.build_advice(items)
    assert a["safety"] and "112" in a["safety"]
    assert any("самоповреждение" in x.lower() or "безопасност" in x.lower() for x in a["avoid"] + a["questions"])


def test_deception_block_has_disclaimer():
    e = Engine()
    tr = []
    for i, (sp, tx) in enumerate([("them", "Я вышел в шесть часов вечера из офиса"),
                                  ("them", "Если честно, я вышел в семь часов вечера из офиса")]):
        u = {"id": i, "speaker": sp, "text": tx, "features": {}}
        u.update(e.analyze_utterance(u))
        tr.append(u)
    a = e.build_advice(tr)
    assert a["deception"] and "не доказательство" in a["deception"][-1]
    assert a["meters"]["deception"] > 0


def test_knowledge_base_integrity():
    books = kb.books()
    assert {b["id"] for b in books} == {"smith", "forward", "thompson", "voss", "cabane"}
    assert kb.technique_count() == 39
    for b in books:
        assert b["core"] and b["techniques"]
        for t in b["techniques"]:
            assert all(t[k] for k in ("id", "name", "what", "how", "example")), (b["id"], t.get("id"))
    assert "Заезженная пластинка" in kb.compact_summary()
    assert kb.guess_book_from_name("Крисс_Восс_Никаких_компромиссов.fb2") == "voss"
    assert kb.guess_book_from_name("Эмоциональный шантаж - Форвард.epub") == "forward"
    assert kb.guess_book_from_name("random.txt") == ""
