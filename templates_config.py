# -*- coding: utf-8 -*-
"""
Relationship templates for Seam.

Seam is a horizontal product: a shared operational layer for ANY recurring B2B
relationship a high-demand business depends on - suppliers, fulfillment,
subcontractors, B2B clients. The thesis is the *neutral middle*, not one
vertical.

So a relationship type is *configuration*, not code:
  - fields:  descriptive attributes of the request (owned by the buyer side)
  - terms:   the agreed terms that require two-sided acceptance (the moat)
  - stages:  the shared status pipeline both sides watch ("where is it?")

The flagship is the GENERAL template - it fits most businesses out of the box.
The named verticals are conveniences for common surfaces. Every terminal stage
is keyed "closed" so the engine treats them uniformly.

Add a relationship type = add a dict here. The engine does not change.
"""

FIELD_TYPES = {"text", "textarea", "number", "money", "date", "select"}

TEMPLATES = {
    # ---- Flagship: fits any high-demand business ---------------------------
    "general": {
        "label": "Общи поръчки и услуги",
        "tagline": "Универсално - за всяка повтаряща се B2B релация",
        "item_noun": "Поръчка",
        "ref_prefix": "REQ",
        "recommended": True,
        "filters": [],
        "fields": [
            {"key": "summary", "label": "Какво се заявява", "type": "text", "required": True, "placeholder": "кратко описание"},
            {"key": "reference", "label": "Ваш референтен №", "type": "text"},
            {"key": "quantity", "label": "Количество / обем", "type": "text"},
            {"key": "details", "label": "Детайли и спецификация", "type": "textarea"},
        ],
        "terms": [
            {"key": "price", "label": "Договорена цена", "type": "money"},
            {"key": "deadline", "label": "Краен срок", "type": "date"},
            {"key": "scope", "label": "Обхват / какво включва", "type": "textarea"},
            {"key": "conditions", "label": "Условия", "type": "textarea"},
        ],
        "stages": [
            {"key": "requested", "label": "Заявена"},
            {"key": "accepted", "label": "Приета"},
            {"key": "in_progress", "label": "В изпълнение"},
            {"key": "review", "label": "Преглед"},
            {"key": "completed", "label": "Изпълнена"},
            {"key": "closed", "label": "Приключена"},
        ],
    },

    # ---- E-commerce ↔ 3PL fulfillment ------------------------------------
    "ecommerce": {
        "label": "E-commerce ↔ 3PL изпълнение",
        "tagline": "Поръчки, изключения, връщания, SLA",
        "item_noun": "Пратка",
        "ref_prefix": "FUL",
        "filters": ["destination"],
        "fields": [
            {"key": "order_ref", "label": "№ на поръчка", "type": "text", "required": True},
            {"key": "sku", "label": "SKU / артикули", "type": "text"},
            {"key": "units", "label": "Брой единици", "type": "number"},
            {"key": "destination", "label": "Дестинация", "type": "text"},
            {"key": "notes", "label": "Забележки", "type": "textarea"},
        ],
        "terms": [
            {"key": "price", "label": "Цена за обработка", "type": "money"},
            {"key": "sla", "label": "SLA / срок за изпращане", "type": "text"},
            {"key": "returns", "label": "Политика за връщания", "type": "textarea"},
        ],
        "stages": [
            {"key": "received", "label": "Получена"},
            {"key": "picking", "label": "Комплектоване"},
            {"key": "shipped", "label": "Изпратена"},
            {"key": "exception", "label": "Изключение"},
            {"key": "delivered", "label": "Доставена"},
            {"key": "closed", "label": "Приключена"},
        ],
    },

    # ---- Agency ↔ client --------------------------------------------------
    "agency": {
        "label": "Агенция ↔ клиент",
        "tagline": "Доставяеми, цикли на одобрение, контрол на обхвата",
        "item_noun": "Доставяем",
        "ref_prefix": "JOB",
        "filters": ["format"],
        "fields": [
            {"key": "deliverable", "label": "Доставяем", "type": "text", "required": True},
            {"key": "format", "label": "Формат", "type": "text"},
            {"key": "brief", "label": "Бриф", "type": "textarea"},
            {"key": "revisions", "label": "Включени корекции", "type": "number"},
        ],
        "terms": [
            {"key": "fee", "label": "Хонорар", "type": "money"},
            {"key": "deadline", "label": "Краен срок", "type": "date"},
            {"key": "scope", "label": "Обхват", "type": "textarea"},
        ],
        "stages": [
            {"key": "requested", "label": "Заявен"},
            {"key": "in_progress", "label": "В изработка"},
            {"key": "in_review", "label": "За преглед"},
            {"key": "revisions", "label": "Корекции"},
            {"key": "approved", "label": "Одобрен"},
            {"key": "closed", "label": "Приключен"},
        ],
    },

    # ---- Construction ↔ subcontractor ------------------------------------
    "construction": {
        "label": "Строителство ↔ подизпълнители",
        "tagline": "Обхват, етапи, приемане, освобождаване на плащане",
        "item_noun": "Възлагане",
        "ref_prefix": "SUB",
        "filters": ["location"],
        "fields": [
            {"key": "work_item", "label": "Вид работа", "type": "text", "required": True},
            {"key": "location", "label": "Обект / локация", "type": "text"},
            {"key": "spec", "label": "Спецификация", "type": "textarea"},
        ],
        "terms": [
            {"key": "price", "label": "Договорена стойност", "type": "money"},
            {"key": "milestone_date", "label": "Срок / етап", "type": "date"},
            {"key": "payment_terms", "label": "Условия за плащане", "type": "text"},
            {"key": "scope", "label": "Обхват", "type": "textarea"},
        ],
        "stages": [
            {"key": "requested", "label": "Заявено"},
            {"key": "quoted", "label": "Оферирано"},
            {"key": "in_progress", "label": "В изпълнение"},
            {"key": "inspection", "label": "Проверка"},
            {"key": "signed_off", "label": "Приета работа"},
            {"key": "closed", "label": "Приключено"},
        ],
    },

    # ---- Restaurant ↔ distributor ----------------------------------------
    "restaurant": {
        "label": "Ресторант ↔ дистрибутор",
        "tagline": "Стокови поръчки, замени, потвърждение на доставка",
        "item_noun": "Поръчка",
        "ref_prefix": "ORD",
        "filters": ["unit", "substitution"],
        "fields": [
            {"key": "product", "label": "Продукт", "type": "text", "required": True},
            {"key": "quantity", "label": "Количество", "type": "number"},
            {"key": "unit", "label": "Мерна единица", "type": "select", "options": ["кг", "бр", "стек", "литър"]},
            {"key": "substitution", "label": "Допустима замяна?", "type": "select", "options": ["Не", "Да - с уведомление"]},
            {"key": "notes", "label": "Забележки", "type": "textarea"},
        ],
        "terms": [
            {"key": "price", "label": "Договорена цена", "type": "money"},
            {"key": "delivery_window", "label": "Прозорец за доставка", "type": "text"},
            {"key": "quality_spec", "label": "Изисквания за качество", "type": "textarea"},
        ],
        "stages": [
            {"key": "ordered", "label": "Заявена"},
            {"key": "confirmed", "label": "Потвърдена"},
            {"key": "packed", "label": "Подготвена"},
            {"key": "out", "label": "Към доставка"},
            {"key": "delivered", "label": "Доставена"},
            {"key": "closed", "label": "Приключена"},
        ],
    },

    # ---- Agriculture: producer <-> buyer / cooperative / input supplier ----
    "agriculture": {
        "label": "Земеделие ↔ изкупвач",
        "tagline": "Реколта, партиди, качество, транспорт от полето",
        "item_noun": "Партида",
        "ref_prefix": "AGR",
        "filters": ["culture", "quality"],
        "fields": [
            {"key": "culture", "label": "Култура", "type": "select", "required": True,
             "options": ["Пшеница", "Царевица", "Слънчоглед", "Рапица", "Ечемик", "Плодове", "Зеленчуци", "Друго"]},
            {"key": "quantity_t", "label": "Количество (тона)", "type": "number"},
            {"key": "harvest_year", "label": "Реколта (година)", "type": "text"},
            {"key": "parcel", "label": "Масив / парцел (БЗС)", "type": "text"},
            {"key": "quality", "label": "Качествени показатели", "type": "textarea"},
            {"key": "storage", "label": "Съхранение / склад", "type": "text"},
        ],
        "terms": [
            {"key": "price", "label": "Договорена цена (за тон)", "type": "money"},
            {"key": "delivery_term", "label": "Условие на доставка (франко)", "type": "text"},
            {"key": "quality_spec", "label": "Приемни качествени норми", "type": "textarea"},
            {"key": "payment_terms", "label": "Условия за плащане", "type": "text"},
        ],
        "stages": [
            {"key": "planned", "label": "Договорена"},
            {"key": "harvested", "label": "Прибрана"},
            {"key": "stored", "label": "На склад"},
            {"key": "sampled", "label": "Проба / анализ"},
            {"key": "shipped", "label": "Натоварена"},
            {"key": "accepted", "label": "Приета"},
            {"key": "closed", "label": "Приключена"},
        ],
    },

    # ---- Auto-import (one vertical among many) ----------------------------
    "auto-import": {
        "label": "Авто-импорт",
        "tagline": "Дилър ↔ доставчик ↔ транспорт",
        "item_noun": "Автомобил",
        "ref_prefix": "CAR",
        "filters": ["source"],
        "fields": [
            {"key": "vehicle", "label": "Автомобил (марка и модел)", "type": "text", "required": True, "placeholder": "напр. BMW 320d"},
            {"key": "year", "label": "Година", "type": "number"},
            {"key": "vin", "label": "VIN / номер на шаси", "type": "text"},
            {"key": "mileage", "label": "Пробег (км)", "type": "number"},
            {"key": "source", "label": "Източник (търг / търговец)", "type": "text"},
            {"key": "condition", "label": "Състояние и забележки", "type": "textarea"},
        ],
        "terms": [
            {"key": "price", "label": "Договорена цена", "type": "money"},
            {"key": "deposit", "label": "Депозит", "type": "money"},
            {"key": "delivery_eta", "label": "Срок за доставка", "type": "date"},
            {"key": "inspection", "label": "Договорено състояние при доставка", "type": "text"},
            {"key": "scope", "label": "Какво включва (обхват)", "type": "textarea"},
        ],
        "stages": [
            {"key": "requested", "label": "Заявен"},
            {"key": "confirmed", "label": "Потвърден"},
            {"key": "paperwork", "label": "Документи"},
            {"key": "payment", "label": "Плащане"},
            {"key": "transport", "label": "Транспорт"},
            {"key": "delivered", "label": "Доставен"},
            {"key": "closed", "label": "Приключен"},
        ],
    },
}


def get_template(key):
    return TEMPLATES.get(key)


def stage_keys(key):
    t = TEMPLATES.get(key, {})
    return [s["key"] for s in t.get("stages", [])]


def stage_label(template_key, stage_key):
    for s in TEMPLATES.get(template_key, {}).get("stages", []):
        if s["key"] == stage_key:
            return s["label"]
    return stage_key


def field_label(template_key, fkey):
    t = TEMPLATES.get(template_key, {})
    for f in t.get("fields", []) + t.get("terms", []):
        if f["key"] == fkey:
            return f["label"]
    return fkey


def public_templates():
    """What the frontend needs to render forms and pipelines."""
    return {k: v for k, v in TEMPLATES.items()}
