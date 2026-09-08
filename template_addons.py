# -*- coding: utf-8 -*-
"""Add-ons: the depth a trade needs, switched on by the company that needs it.

`templates_config.py` deliberately keeps every template thin. A thin template
is what makes the first record easy to open, and it is why the general one
fits almost everybody. But a construction firm actually issues акт образец 19
with a retention percentage, a car importer actually has an MRN and an
ecotax, and an online shop actually has returns and cash on delivery. Putting
all of that into the base templates would hand every user a form of sixty
fields, nine tenths of which are somebody else's trade.

So depth is opt in. An add-on is a coherent unit of one real practice, and it
carries everything that practice needs at once:

* **fields** the buying side fills in,
* **terms** that need both signatures,
* **stages** inserted at a named point in the existing pipeline,
* **documents** it makes available,
* **metrics** it makes measurable.

A company switches on the four or five that match how it actually works, and
the forms, the pipeline and the analytics all follow from that one choice.

Three rules keep this safe, and `self_test` enforces all of them:

1. An add-on may never define a key that its base template already uses, and
   two add-ons that can be switched on together may never share a key. Order
   fields are stored by key: a collision would silently overwrite real data.
2. A stage is inserted **after** a named existing stage, never before the
   first and never after the terminal one. The engine treats "closed" as
   terminal everywhere, so nothing may come after it.
3. Every metric refers to a stage or field that exists once its own add-on is
   applied, or the analytics screen offers a figure it cannot compute.

No Flask, no database, no translation. Run this file to check it.
"""

import re
from datetime import datetime, timedelta

from templates_config import TEMPLATES, FIELD_TYPES

#: Which broad area an add-on belongs to, for grouping in the picker.
GROUPS = ("commerce", "logistics", "construction", "vehicles",
          "finance", "quality", "legal", "people")

#: How a metric is worked out. Deliberately few shapes: every one of these can
#: be computed from records, their stage history and their agreed terms, with
#: no per-add-on code. A shape that cannot be computed is not offered.
METRIC_KINDS = ("count", "sum", "avg", "days_between", "ratio")


def _f(key, label, type_="text", **kw):
    d = {"key": key, "label": label, "type": type_}
    d.update(kw)
    return d


ADDONS = {

    # ======================================================================= #
    #  Online trade
    # ======================================================================= #
    "returns": {
        "label": "Връщания и рекламации",
        "tagline": "Срок за връщане, състояние, кой поема разходите",
        "group": "commerce",
        "templates": ("ecommerce", "general", "restaurant"),
        "sectors": ("ecommerce", "general"),
        "fields": [
            _f("rma_number", "Номер на рекламацията"),
            _f("return_reason", "Причина за връщане", "select", options=["mismatch", "defect", "transit_damage", "wrong_item", "withdrawal", "other"]),
            _f("return_condition", "Състояние на върнатото", "select", options=["unopened", "resalable", "not_resalable", "damaged"]),
            _f("return_qty", "Върнато количество", "number"),
        ],
        "terms": [
            _f("return_window_days", "Срок за връщане (дни)", "number"),
            _f("return_shipping_by", "Транспортът при връщане е за сметка на", "select",
               options=["buyer", "seller", "at_fault"]),
            _f("restock_fee", "Такса за възстановяване на склад", "money"),
            _f("refund_days", "Срок за възстановяване на сумата (дни)", "number"),
        ],
        "stages": [{"key": "returned", "label": "Върната", "after": "delivered"}],
        "docs": ("claim",),
        "metrics": [
            {"key": "return_rate", "label": "Дял на върнатите", "kind": "ratio",
             "of": {"stage_reached": "returned"}, "over": {"any": True}, "unit": "%"},
            {"key": "refund_value", "label": "Стойност на възстановеното", "kind": "sum",
             "field": "restock_fee", "unit": "money"},
        ],
    },

    "cod": {
        "label": "Наложен платеж",
        "tagline": "Събиране от куриера, такса, срок за превод",
        "group": "finance",
        "templates": ("ecommerce", "general", "restaurant", "agriculture"),
        "sectors": ("ecommerce", "general", "restaurant"),
        "fields": [
            _f("cod_amount", "Сума за събиране", "money"),
            _f("cod_collector", "Събира се от", "text", placeholder="куриер или превозвач"),
            _f("cod_collected_on", "Събрана на", "date"),
        ],
        "terms": [
            _f("cod_fee", "Такса за наложен платеж", "money"),
            _f("cod_remit_days", "Превод към продавача до (дни)", "number"),
        ],
        "metrics": [
            {"key": "cod_outstanding", "label": "Събрано, но непреведено", "kind": "sum",
             "field": "cod_amount", "where": {"field_set": "cod_collected_on"}, "unit": "money"},
        ],
    },

    "stock": {
        "label": "Складова наличност и партиди",
        "tagline": "Артикул, партида, срок на годност, местоположение",
        "group": "logistics",
        "templates": ("ecommerce", "general", "restaurant", "agriculture"),
        "sectors": ("ecommerce", "restaurant", "agriculture"),
        "fields": [
            # Not "sku": the e-commerce template already has one, and a second
            # field under the same key would overwrite it on save.
            _f("stock_sku", "Артикулен номер (склад)"),
            _f("batch", "Партида"),
            _f("expiry", "Срок на годност", "date"),
            _f("warehouse_location", "Местоположение в склада"),
            _f("stock_on_hand", "Наличност към момента", "number"),
        ],
        "terms": [
            _f("min_shelf_life_days", "Минимален остатъчен срок при доставка (дни)", "number"),
            _f("stock_cover_days", "Поддържана наличност (дни)", "number"),
        ],
    },

    "marketplace": {
        "label": "Маркетплейс канали",
        "tagline": "Външен канал, номер на поръчката там, комисиона",
        "group": "commerce",
        "templates": ("ecommerce", "general"),
        "sectors": ("ecommerce",),
        "fields": [
            _f("channel", "Канал", "select", options=["own_site", "emag", "amazon", "ebay", "allegro", "olx", "facebook", "instagram", "other_channel"]),
            _f("channel_order_id", "Номер на поръчката в канала"),
            _f("channel_fee", "Комисиона на канала", "money"),
        ],
        "metrics": [
            {"key": "channel_fees", "label": "Платени комисиони", "kind": "sum",
             "field": "channel_fee", "unit": "money"},
        ],
    },

    "sla": {
        "label": "SLA и неустойки",
        "tagline": "Срокове за обработка, отговорност при закъснение и щета",
        "group": "legal",
        "templates": ("ecommerce", "general", "agency", "construction"),
        "sectors": ("ecommerce", "general", "agency", "construction"),
        "terms": [
            _f("sla_hours", "Срок за обработка (часа)", "number"),
            _f("late_penalty", "Неустойка за забава", "money"),
            _f("penalty_cap", "Таван на неустойките", "money"),
            _f("damage_liability", "Отговорност при повреда", "textarea"),
            _f("force_majeure", "Непреодолима сила", "textarea"),
        ],
        "docs": ("claim",),
    },

    "customs_goods": {
        "label": "Внос от трети страни",
        "tagline": "Тарифен код, произход, мита, Incoterms",
        "group": "logistics",
        "templates": ("ecommerce", "general", "agriculture"),
        "sectors": ("ecommerce", "general", "agriculture"),
        "fields": [
            _f("hs_code", "Тарифен код (HS/CN)"),
            _f("origin_country", "Страна на произход"),
            _f("customs_value", "Митническа стойност", "money"),
            _f("mrn", "MRN на декларацията"),
        ],
        "terms": [
            _f("incoterm", "Incoterms", "select", options=["exw", "fca", "fas", "fob", "cfr", "cif", "cpt", "cip", "dap", "dpu", "ddp"]),
            _f("duty_payer", "Мита и ДДС при внос за сметка на", "select",
               options=["buyer", "seller"]),
        ],
        "stages": [{"key": "customs", "label": "Митница", "after": "accepted"}],
    },

    "packaging": {
        "label": "Опаковане и брандиране",
        "tagline": "Спецификация на опаковката и вложките",
        "group": "commerce",
        "templates": ("ecommerce", "general", "agriculture"),
        "sectors": ("ecommerce", "agriculture"),
        "fields": [
            _f("packaging_spec", "Изисквания към опаковката", "textarea"),
            _f("insert_material", "Вложки и рекламни материали", "textarea"),
            _f("label_language", "Език на етикета"),
        ],
    },

    # ======================================================================= #
    #  Construction
    # ======================================================================= #
    "act19": {
        "label": "Актуване (обр. 19)",
        "tagline": "Междинни актове, измерени количества, задържане",
        "group": "construction",
        "templates": ("construction", "general"),
        "sectors": ("construction",),
        "fields": [
            _f("act_number", "Номер на акта"),
            _f("act_period", "Период на актуване"),
            _f("measured_qty", "Измерено количество"),
            _f("measured_value", "Стойност по акта", "money"),
        ],
        "terms": [
            _f("retention_pct", "Задържана гаранция (%)", "number"),
            _f("act_cycle_days", "Период на актуване (дни)", "number"),
            _f("retention_release", "Освобождаване на задържаното", "textarea"),
        ],
        "stages": [{"key": "acted", "label": "Актувано", "after": "in_progress"}],
        "docs": ("protocol",),
        "metrics": [
            {"key": "acted_value", "label": "Актувана стойност", "kind": "sum",
             "field": "measured_value", "unit": "money"},
            {"key": "days_to_act", "label": "Дни до актуване", "kind": "days_between",
             "from": "in_progress", "to": "acted", "unit": "days"},
        ],
    },

    "hidden_works": {
        "label": "Скрити работи (обр. 12)",
        "tagline": "Приемане преди закриване на конструкцията",
        "group": "construction",
        "templates": ("construction",),
        "sectors": ("construction",),
        "fields": [
            _f("hidden_act_no", "Номер на акта за скрити работи"),
            _f("hidden_scope", "Какво се закрива", "textarea"),
            _f("inspected_by", "Приел от страна на възложителя"),
            _f("inspected_on", "Дата на приемане", "date"),
        ],
        "docs": ("protocol",),
    },

    "safety": {
        "label": "Безопасност и здраве",
        "tagline": "План, инструктаж, лични предпазни средства",
        "group": "people",
        "templates": ("construction", "general", "agriculture"),
        "sectors": ("construction", "agriculture"),
        "fields": [
            _f("safety_plan", "План по безопасност", "textarea"),
            _f("briefing_date", "Дата на инструктажа", "date"),
            _f("ppe_issued", "Издадени ЛПС", "textarea"),
            _f("site_incidents", "Регистрирани инциденти", "number"),
        ],
        "terms": [
            _f("safety_liability", "Отговорност по ЗБУТ", "textarea"),
            _f("safety_coordinator", "Координатор по безопасност"),
        ],
        "metrics": [
            {"key": "incidents", "label": "Инциденти", "kind": "sum",
             "field": "site_incidents", "unit": "count"},
        ],
    },

    "warranty": {
        "label": "Гаранционни срокове",
        "tagline": "Срок по видове работи и срок за реакция при дефект",
        "group": "quality",
        "templates": ("construction", "general", "auto-import"),
        "sectors": ("construction", "auto-import", "general"),
        "terms": [
            _f("warranty_years", "Гаранционен срок (години)", "number"),
            _f("warranty_scope", "Обхват на гаранцията", "textarea"),
            _f("defect_response_hours", "Срок за реакция при дефект (часа)", "number"),
            _f("defect_fix_days", "Срок за отстраняване (дни)", "number"),
        ],
        "docs": ("claim",),
    },

    "plant": {
        "label": "Механизация и техника",
        "tagline": "Машина, отработени часове, оператор, гориво",
        "group": "construction",
        "templates": ("construction", "agriculture", "general"),
        "sectors": ("construction", "agriculture"),
        "fields": [
            _f("machine", "Машина"),
            _f("machine_hours", "Отработени часове", "number"),
            _f("operator", "Оператор"),
        ],
        "terms": [
            _f("hourly_rate", "Часова ставка", "money"),
            _f("fuel_by", "Горивото е за сметка на", "select",
               options=["contractor", "client"]),
            _f("mobilisation_fee", "Такса за докарване", "money"),
        ],
        "metrics": [
            {"key": "machine_hours_total", "label": "Отработени машиночасове", "kind": "sum",
             "field": "machine_hours", "unit": "count"},
        ],
    },

    "materials": {
        "label": "Материали и сертификати",
        "tagline": "Влаган материал, количество, декларация за характеристики",
        "group": "quality",
        "templates": ("construction", "general", "agriculture"),
        "sectors": ("construction", "agriculture"),
        "fields": [
            _f("material", "Материал"),
            _f("material_qty", "Количество"),
            _f("material_unit", "Мерна единица", "select",
               options=["unit_pcs", "unit_kg", "unit_t", "unit_m", "unit_m2", "unit_m3", "unit_l", "unit_pack"]),
            _f("dop_number", "Декларация за експлоатационни показатели (DoP)"),
        ],
        "terms": [
            _f("material_supplied_by", "Материалите се осигуряват от", "select",
               options=["contractor", "client"]),
        ],
    },

    "site_access": {
        "label": "Достъп до обекта",
        "tagline": "Адрес, работно време, кой отключва",
        "group": "logistics",
        "templates": ("construction", "general"),
        "sectors": ("construction",),
        "fields": [
            _f("site_address", "Адрес на обекта"),
            _f("access_window", "Работно време на обекта"),
            _f("keyholder", "Отговорник за достъпа"),
        ],
        "docs": ("site",),
    },

    "progress_payment": {
        "label": "Междинни плащания",
        "tagline": "Аванс, периодичност, освобождаване на задържаното",
        "group": "finance",
        "templates": ("construction", "agency", "general"),
        "sectors": ("construction", "agency"),
        "terms": [
            _f("advance_pct", "Аванс (%)", "number"),
            _f("payment_cycle_days", "Периодичност на плащане (дни)", "number"),
            _f("payment_condition", "Условие за плащане", "textarea"),
        ],
    },

    # ======================================================================= #
    #  Vehicle import
    # ======================================================================= #
    "customs_vehicle": {
        "label": "Митническо оформяне",
        "tagline": "Митница, MRN, стойност, кой плаща митата",
        "group": "vehicles",
        "templates": ("auto-import",),
        "sectors": ("auto-import",),
        "fields": [
            _f("customs_office", "Митническо учреждение"),
            _f("vehicle_mrn", "MRN на декларацията"),
            _f("vehicle_customs_value", "Митническа стойност", "money"),
            _f("cleared_on", "Освободен на", "date"),
        ],
        "terms": [
            _f("vehicle_duty_payer", "Мита и ДДС за сметка на", "select",
               options=["buyer", "seller"]),
            _f("clearance_days", "Срок за оформяне (дни)", "number"),
        ],
        "stages": [{"key": "customs", "label": "Митница", "after": "paperwork"}],
        "metrics": [
            {"key": "days_to_clear", "label": "Дни до митническо освобождаване",
             "kind": "days_between", "from": "paperwork", "to": "customs", "unit": "days"},
        ],
    },

    "registration": {
        "label": "Регистрация на автомобила",
        "tagline": "Табели, държава на регистрация, кой я извършва",
        "group": "vehicles",
        "templates": ("auto-import",),
        "sectors": ("auto-import",),
        "fields": [
            _f("plate", "Регистрационен номер"),
            _f("reg_country", "Държава на регистрация"),
            _f("reg_date", "Дата на регистрация", "date"),
            _f("first_reg_date", "Първа регистрация", "date"),
        ],
        "terms": [
            _f("reg_by", "Регистрацията се извършва от", "select",
               options=["buyer", "seller", "attorney"]),
            _f("reg_fee", "Такси по регистрацията", "money"),
        ],
        "stages": [{"key": "registered", "label": "Регистриран", "after": "delivered"}],
        "docs": ("poa",),
    },

    "ecotax": {
        "label": "Екотакса и данъци",
        "tagline": "Емисии, екологична категория, данъчна основа",
        "group": "vehicles",
        "templates": ("auto-import",),
        "sectors": ("auto-import",),
        "fields": [
            _f("co2", "Емисии CO2 (г/км)", "number"),
            _f("euro_standard", "Екологична категория", "select",
               options=["euro1", "euro2", "euro3", "euro4", "euro5", "euro6", "electric"]),
            _f("engine_cc", "Обем на двигателя (см³)", "number"),
            _f("power_kw", "Мощност (kW)", "number"),
        ],
        "terms": [
            _f("ecotax_amount", "Екотакса", "money"),
            _f("vehicle_tax_payer", "Данъците за сметка на", "select",
               options=["buyer", "seller"]),
        ],
    },

    "vehicle_transport": {
        "label": "Транспорт с автовоз",
        "tagline": "Превозвач, място на натоварване, срок",
        "group": "logistics",
        "templates": ("auto-import",),
        "sectors": ("auto-import",),
        "fields": [
            _f("carrier", "Превозвач"),
            _f("loading_point", "Място на натоварване"),
            _f("unloading_point", "Място на разтоварване"),
            _f("loaded_on", "Натоварен на", "date"),
        ],
        "terms": [
            _f("transport_price", "Цена на транспорта", "money"),
            _f("transport_insurance", "Застраховка на превоза", "select",
               options=["included", "separate", "none"]),
        ],
        "metrics": [
            {"key": "transport_cost", "label": "Разходи за транспорт", "kind": "sum",
             "field": "transport_price", "unit": "money"},
            {"key": "days_in_transit", "label": "Дни в транспорт", "kind": "days_between",
             "from": "transport", "to": "delivered", "unit": "days"},
        ],
    },

    "vehicle_inspection": {
        "label": "Технически преглед и диагностика",
        "tagline": "Проверен пробег, установени дефекти, заключение",
        "group": "quality",
        "templates": ("auto-import",),
        "sectors": ("auto-import",),
        "fields": [
            _f("inspection_date", "Дата на прегледа", "date"),
            _f("odometer_verified", "Пробегът е проверен", "select",
               options=["yes", "no", "undetermined"]),
            _f("defects_found", "Установени дефекти", "textarea"),
            _f("inspection_result", "Заключение", "select",
               options=["pass", "pass_notes", "fail"]),
        ],
        "docs": ("protocol",),
    },

    "vehicle_damage": {
        "label": "Щети и експертиза",
        "tagline": "Описание, експертна оценка, кой поема",
        "group": "quality",
        "templates": ("auto-import", "general"),
        "sectors": ("auto-import",),
        "fields": [
            _f("damage_desc", "Описание на щетата", "textarea"),
            _f("damage_found_on", "Установена на", "date"),
            _f("expert_report", "Номер на експертизата"),
            _f("repair_estimate", "Оценка за ремонт", "money"),
        ],
        "terms": [
            _f("damage_borne_by", "Щетата е за сметка на", "select",
               options=["buyer", "seller", "carrier", "insurer"]),
        ],
        "docs": ("claim",),
        "metrics": [
            {"key": "damage_cost", "label": "Оценени щети", "kind": "sum",
             "field": "repair_estimate", "unit": "money"},
        ],
    },

    "vehicle_history": {
        "label": "История на автомобила",
        "tagline": "Собственици, произшествия, справка",
        "group": "vehicles",
        "templates": ("auto-import",),
        "sectors": ("auto-import",),
        "fields": [
            _f("history_ref", "Номер на справката"),
            _f("owners_count", "Брой предишни собственици", "number"),
            _f("accident_history", "Данни за произшествия", "textarea"),
            _f("service_history", "Сервизна история", "select",
               options=["full", "partial", "missing"]),
        ],
    },

    "financing": {
        "label": "Лизинг и финансиране",
        "tagline": "Финансираща институция, вноски, оскъпяване",
        "group": "finance",
        "templates": ("auto-import", "general", "construction"),
        "sectors": ("auto-import", "construction", "general"),
        "fields": [
            _f("lender", "Финансираща институция"),
            _f("finance_contract_no", "Номер на договора"),
        ],
        "terms": [
            _f("down_payment", "Първоначална вноска", "money"),
            _f("finance_months", "Срок (месеци)", "number"),
            _f("finance_rate", "Годишен процент на разходите (%)", "number"),
            _f("monthly_instalment", "Месечна вноска", "money"),
        ],
    },

    # ======================================================================= #
    #  Cross-trade
    # ======================================================================= #
    "quality_control": {
        "label": "Контрол на качеството",
        "tagline": "Метод на проверка, допустимо отклонение, кой приема",
        "group": "quality",
        "templates": ("general", "ecommerce", "construction", "agriculture", "restaurant"),
        "sectors": ("general", "ecommerce", "construction", "agriculture", "restaurant"),
        "fields": [
            _f("qc_method", "Метод на проверка", "select", options=["full_check", "sample", "documents", "lab"]),
            _f("qc_sample_size", "Размер на извадката", "number"),
            _f("qc_result", "Резултат", "select",
               options=["accepted", "accepted_notes", "rejected"]),
            _f("qc_notes", "Забележки", "textarea"),
        ],
        "terms": [
            _f("qc_tolerance", "Допустимо отклонение", "text"),
            _f("qc_accepted_by", "Приема се от"),
        ],
        "docs": ("protocol",),
        "metrics": [
            {"key": "reject_rate", "label": "Дял на отхвърлените", "kind": "ratio",
             "of": {"field_equals": ["qc_result", "Отхвърлен"]}, "over": {"any": True},
             "unit": "%"},
        ],
    },

    "confidentiality": {
        "label": "Поверителност и лични данни",
        "tagline": "Какво е поверително, за колко време, обработка на данни",
        "group": "legal",
        "templates": ("general", "agency", "ecommerce", "construction", "auto-import",
                      "restaurant", "agriculture"),
        "sectors": ("general", "agency", "ecommerce", "construction", "auto-import"),
        "terms": [
            _f("nda_scope", "Обхват на поверителността", "textarea"),
            _f("nda_years", "Срок на поверителността (години)", "number"),
            _f("data_processing", "Обработка на лични данни", "textarea"),
        ],
        "docs": ("nda", "gdpr"),
    },

    "insurance": {
        "label": "Застраховане",
        "tagline": "Полица, покритие, кой я поддържа",
        "group": "legal",
        "templates": ("general", "construction", "auto-import", "agriculture"),
        "sectors": ("construction", "auto-import", "agriculture", "general"),
        "fields": [
            _f("policy_no", "Номер на полицата"),
            _f("insurer", "Застраховател"),
            _f("policy_valid_to", "Валидна до", "date"),
        ],
        "terms": [
            _f("insurance_cover", "Покритие", "money"),
            _f("insurance_by", "Застраховката се поддържа от", "select",
               options=["contractor", "client"]),
        ],
    },

    "people_onsite": {
        "label": "Екип на място",
        "tagline": "Отговорник, брой хора, квалификация",
        "group": "people",
        "templates": ("general", "construction", "agency", "restaurant", "agriculture"),
        "sectors": ("construction", "agency", "agriculture", "restaurant"),
        "fields": [
            _f("crew_lead", "Отговорник на екипа"),
            _f("crew_size", "Брой хора", "number"),
            _f("qualifications", "Изисквана квалификация", "textarea"),
        ],
    },

    "cold_chain": {
        "label": "Хладилна верига",
        "tagline": "Температурен режим, измерване, допустимо отклонение",
        "group": "logistics",
        "templates": ("restaurant", "agriculture", "ecommerce", "general"),
        "sectors": ("restaurant", "agriculture"),
        "fields": [
            _f("temp_required", "Изискван режим (°C)"),
            _f("temp_on_arrival", "Температура при доставка (°C)", "number"),
            _f("temp_log", "Записи от температурния дневник", "textarea"),
        ],
        "terms": [
            _f("temp_tolerance", "Допустимо отклонение (°C)", "number"),
            _f("temp_breach_action", "Действие при нарушен режим", "textarea"),
        ],
    },
}


# --------------------------------------------------------------------------- #
#  Resolution
# --------------------------------------------------------------------------- #
def available(template_key):
    """Every add-on that fits this template, in catalogue order."""
    return [k for k, a in ADDONS.items() if template_key in a["templates"]]


def for_sector(sector):
    return [k for k, a in ADDONS.items() if sector in a.get("sectors", ())]


def by_group(template_key):
    """The available add-ons arranged by group, for a picker that is not one
    flat list of thirty checkboxes."""
    out = {}
    for key in available(template_key):
        out.setdefault(ADDONS[key]["group"], []).append(key)
    return out


def _insert_stage(stages, stage):
    """Put a stage after the one it names.

    An unknown anchor puts it before the terminal stage rather than at the
    end: appending after "closed" would create a pipeline with something to do
    after the record is finished, which the engine treats as unreachable.
    """
    out = list(stages)
    at = None
    for i, s in enumerate(out):
        if s["key"] == stage.get("after"):
            at = i + 1
            break
    if at is None:
        at = max(0, len(out) - 1)
    out.insert(at, {"key": stage["key"], "label": stage["label"]})
    return out


def resolve(template_key, enabled=()):
    """The template as a company that switched these add-ons on will see it.

    Returns a fresh dict every time. Unknown or unfitting keys are ignored
    rather than raising: an add-on removed from the catalogue must not make
    every existing record in that workspace unopenable.
    """
    base = TEMPLATES.get(template_key)
    if not base:
        return None
    out = dict(base)
    out["fields"] = list(base.get("fields", []))
    out["terms"] = list(base.get("terms", []))
    out["stages"] = list(base.get("stages", []))
    out["docs"] = list(base.get("docs", ()))
    out["metrics"] = list(base.get("metrics", ()))
    out["addons"] = []
    fits = set(available(template_key))
    for key in enabled or ():
        if key not in fits:
            continue
        a = ADDONS[key]
        out["addons"].append(key)
        out["fields"].extend(a.get("fields", []))
        out["terms"].extend(a.get("terms", []))
        for st in a.get("stages", []):
            out["stages"] = _insert_stage(out["stages"], st)
        for d in a.get("docs", ()):
            if d not in out["docs"]:
                out["docs"].append(d)
        for m in a.get("metrics", []):
            out["metrics"].append(dict(m, addon=key))
    return out


def metrics_for(template_key, enabled=()):
    r = resolve(template_key, enabled)
    return r["metrics"] if r else []


def field_label(template_key, enabled, fkey):
    """A label for a key that may come from the base or from an add-on."""
    r = resolve(template_key, enabled) or {}
    for f in r.get("fields", []) + r.get("terms", []):
        if f["key"] == fkey:
            return f["label"]
    return fkey


# --------------------------------------------------------------------------- #
#  Working the figures out
#
#  Five shapes, deliberately. Every one of them can be computed from records,
#  their stage history and their agreed terms with no per-add-on code, which is
#  why an add-on may only declare a metric in one of these shapes: a figure the
#  engine cannot compute is a figure the screen would have to invent.
# --------------------------------------------------------------------------- #
#: Below this many records a figure is not shown. A completion rate over two
#: records is not a rate, and a confident-looking number drawn from nothing is
#: worse than an empty panel: somebody will quote it in a meeting.
MIN_SAMPLE = 3


def _num(value):
    """A number out of whatever was typed. Money arrives as "1 250,00 лв." and
    as "1,250.00"; both mean the same and neither is a float yet."""
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value or "").strip()
    if not s:
        return None
    s = re.sub(r"[^\d,.\-]", "", s)
    # A trailing separator is punctuation, not a decimal point: "1 250,00 лв."
    # keeps the full stop of the abbreviation, and reading that as the decimal
    # mark turns 1250 into 125000.
    s = s.strip(".,")
    if not s or s in ("-",):
        return None
    # The last separator is the decimal one; everything before it groups.
    last_dot, last_comma = s.rfind("."), s.rfind(",")
    if last_dot >= 0 and last_comma >= 0:
        dec = max(last_dot, last_comma)
        s = re.sub(r"[.,]", "", s[:dec]) + "." + s[dec + 1:]
    elif last_comma >= 0:
        # A lone comma is a decimal point unless it groups thousands exactly.
        s = s.replace(",", "" if re.match(r"^-?\d{1,3}(,\d{3})+$", s) else ".")
    try:
        return float(s)
    except ValueError:
        return None


def _value_of(record, key):
    """Fields and agreed terms share one namespace on the form, so a metric
    naming a key should not have to say which of the two it lives in."""
    for bag in ("fields", "terms"):
        got = (record.get(bag) or {}).get(key)
        if got not in (None, ""):
            return got
    return None


def _matches(record, where):
    """Whether one record counts towards a metric."""
    if not where or where.get("any"):
        return True
    if "stage_reached" in where:
        return where["stage_reached"] in (record.get("reached") or {})
    if "field_set" in where:
        return _value_of(record, where["field_set"]) is not None
    if "field_equals" in where:
        key, want = where["field_equals"]
        return _value_of(record, key) == want
    return False


def compute(metric, records):
    """One figure, or None when there is not enough to say anything.

    Always returns the sample size alongside it. A percentage without the
    count behind it is the most quotable kind of wrong number.
    """
    out = {"key": metric["key"], "label": metric.get("label", metric["key"]),
           "unit": metric.get("unit", ""), "kind": metric["kind"],
           "addon": metric.get("addon", ""), "value": None, "n": 0}
    kind = metric["kind"]
    pool = [r for r in records if _matches(r, metric.get("where"))]

    if kind == "count":
        out["n"] = len(records)
        out["value"] = len(pool)
        return out

    if kind in ("sum", "avg"):
        nums = [n for n in (_num(_value_of(r, metric["field"])) for r in pool) if n is not None]
        out["n"] = len(nums)
        if nums and (kind == "sum" or len(nums) >= MIN_SAMPLE):
            out["value"] = round(sum(nums), 2) if kind == "sum" \
                else round(sum(nums) / len(nums), 1)
        return out

    if kind == "days_between":
        spans = []
        for r in records:
            reached = r.get("reached") or {}
            a, b = reached.get(metric["from"]), reached.get(metric["to"])
            if a and b and b >= a:
                spans.append((b - a).total_seconds() / 86400.0)
        out["n"] = len(spans)
        if len(spans) >= MIN_SAMPLE:
            out["value"] = round(sum(spans) / len(spans), 1)
        return out

    if kind == "ratio":
        over = [r for r in records if _matches(r, metric.get("over") or {"any": True})]
        of = [r for r in over if _matches(r, metric.get("of") or {})]
        out["n"] = len(over)
        if len(over) >= MIN_SAMPLE:
            out["value"] = round(len(of) * 100.0 / len(over), 1)
        return out

    return out


def report(template_key, enabled, records):
    """Every figure this configuration makes measurable, in catalogue order."""
    return [compute(m, records) for m in metrics_for(template_key, enabled)]


# --------------------------------------------------------------------------- #
#  Self-test
# --------------------------------------------------------------------------- #
def self_test():
    ok, bad = 0, []

    def eq(got, want, what):
        nonlocal ok
        if got == want:
            ok += 1
        else:
            bad.append("%s: %r != %r" % (what, got, want))

    def yes(cond, what):
        eq(bool(cond), True, what)

    # ---- shape ----------------------------------------------------------- #
    for key, a in ADDONS.items():
        yes(key.replace("_", "").isalnum(), "%s has a usable key" % key)
        for need in ("label", "tagline", "group", "templates", "sectors"):
            yes(a.get(need), "%s declares %s" % (key, need))
        yes(a["group"] in GROUPS, "%s has a real group" % key)
        yes(a.get("fields") or a.get("terms"), "%s actually adds something" % key)
        for t in a["templates"]:
            yes(t in TEMPLATES, "%s targets the real template %s" % (key, t))
        for f in a.get("fields", []) + a.get("terms", []):
            yes(f.get("key") and f.get("label"), "%s field is named" % key)
            yes(f.get("type") in FIELD_TYPES, "%s/%s has a real type" % (key, f.get("key")))
            if f.get("type") == "select":
                yes(len(f.get("options") or []) >= 2, "%s/%s offers choices" % (key, f["key"]))

    # ---- rule 1: no key may ever collide --------------------------------- #
    for tkey, tpl in TEMPLATES.items():
        base_keys = {f["key"] for f in tpl.get("fields", []) + tpl.get("terms", [])}
        seen = {}
        for akey in available(tkey):
            a = ADDONS[akey]
            for f in a.get("fields", []) + a.get("terms", []):
                eq(f["key"] in base_keys, False,
                   "%s/%s does not shadow a base field of %s" % (akey, f["key"], tkey))
                if f["key"] in seen:
                    bad.append("%s/%s collides with %s on %s"
                               % (akey, f["key"], seen[f["key"]], tkey))
                else:
                    ok += 1
                    seen[f["key"]] = akey

    # ---- rule 2: stages land in a legal place ---------------------------- #
    for tkey in TEMPLATES:
        base_stages = [s["key"] for s in TEMPLATES[tkey].get("stages", [])]
        for akey in available(tkey):
            for st in ADDONS[akey].get("stages", []):
                yes(st.get("key") and st.get("label"), "%s stage is named" % akey)
                eq(st["key"] in base_stages, False,
                   "%s/%s is not already a stage of %s" % (akey, st["key"], tkey))
                got = [s["key"] for s in resolve(tkey, [akey])["stages"]]
                eq(got[-1], base_stages[-1],
                   "%s leaves %s terminal on %s" % (akey, base_stages[-1], tkey))
                eq(got[0], base_stages[0], "%s leaves the first stage of %s alone" % (akey, tkey))
                eq(len(got), len(base_stages) + 1, "%s adds exactly one stage to %s" % (akey, tkey))

    # ---- rule 3: metrics refer to something that exists ------------------ #
    for akey, a in ADDONS.items():
        for m in a.get("metrics", []):
            yes(m.get("key") and m.get("label"), "%s metric is named" % akey)
            yes(m.get("kind") in METRIC_KINDS, "%s/%s has a real kind" % (akey, m.get("key")))
            tkey = a["templates"][0]
            r = resolve(tkey, [akey])
            keys = {f["key"] for f in r["fields"] + r["terms"]}
            stages = {s["key"] for s in r["stages"]}
            if m["kind"] in ("sum", "avg"):
                eq(m.get("field") in keys, True,
                   "%s/%s measures a field that exists" % (akey, m["key"]))
            if m["kind"] == "days_between":
                eq(m.get("from") in stages and m.get("to") in stages, True,
                   "%s/%s spans stages that exist" % (akey, m["key"]))
            if m["kind"] == "ratio":
                of = m.get("of") or {}
                if "stage_reached" in of:
                    eq(of["stage_reached"] in stages, True,
                       "%s/%s counts a stage that exists" % (akey, m["key"]))
                if "field_equals" in of:
                    eq(of["field_equals"][0] in keys, True,
                       "%s/%s tests a field that exists" % (akey, m["key"]))

    # ---- resolution ------------------------------------------------------ #
    base = TEMPLATES["construction"]
    r = resolve("construction", ["act19"])
    eq(len(r["fields"]), len(base["fields"]) + 4, "act19 adds its four fields")
    eq(len(r["terms"]), len(base["terms"]) + 3, "and its three terms")
    eq([s["key"] for s in r["stages"]].index("acted"),
       [s["key"] for s in base["stages"]].index("in_progress") + 1,
       "the new stage lands right after the one it names")
    eq(r["addons"], ["act19"], "and the record says which add-on did it")

    eq(resolve("construction", ["nonsense"])["addons"], [], "an unknown add-on is ignored")
    eq(resolve("construction", ["marketplace"])["addons"], [],
       "an add-on for another trade is ignored")
    eq(resolve("nonsense", []), None, "an unknown template resolves to nothing")
    eq(len(resolve("construction", [])["fields"]), len(base["fields"]),
       "nothing switched on changes nothing")
    eq(len(TEMPLATES["construction"]["fields"]), len(base["fields"]),
       "and resolving never mutates the catalogue")

    two = resolve("construction", ["act19", "safety"])
    eq(len(two["fields"]), len(base["fields"]) + 4 + 4, "two add-ons both apply")
    eq(two["addons"], ["act19", "safety"], "in the order they were switched on")

    eq(field_label("construction", ["act19"], "act_number"), "Номер на акта",
       "an add-on field knows its own label")
    eq(field_label("construction", [], "act_number"), "act_number",
       "and is not claimed when the add-on is off")

    # Every template must have something worth switching on, or the picker
    # opens on an empty panel for that trade.
    for tkey in TEMPLATES:
        yes(len(available(tkey)) >= 3, "%s has add-ons worth offering" % tkey)
        yes(by_group(tkey), "%s groups them" % tkey)

    # ---- reading a number out of what people actually type ---------------- #
    eq(_num("1250"), 1250.0, "a plain number")
    eq(_num("1 250,00 лв."), 1250.0, "a Bulgarian amount with a currency word")
    eq(_num("1,250.00"), 1250.0, "an English amount")
    eq(_num("1.250,50"), 1250.5, "a German amount")
    eq(_num("1,250"), 1250.0, "a comma grouping thousands")
    eq(_num("0,5"), 0.5, "a lone comma is a decimal point")
    eq(_num("-42.5"), -42.5, "a negative")
    eq(_num(""), None, "an empty cell is not a zero")
    eq(_num(None), None, "and neither is a missing one")
    eq(_num("няма"), None, "a word is not a number")
    eq(_num("."), None, "nor is a lone separator")
    eq(_num(7), 7.0, "a number stays a number")

    # ---- the five shapes -------------------------------------------------- #
    d0 = datetime(2026, 6, 1)
    def rec(fields=None, terms=None, reached=None):
        return {"fields": fields or {}, "terms": terms or {}, "reached": reached or {}}

    money = [rec(fields={"measured_value": "1 000,00"}),
             rec(fields={"measured_value": "2 000,00"}),
             rec(fields={"measured_value": "3 000,00"}),
             rec(fields={"measured_value": ""})]
    got = compute({"key": "v", "kind": "sum", "field": "measured_value"}, money)
    eq(got["value"], 6000.0, "a sum adds what is there")
    eq(got["n"], 3, "and reports how many rows it added")
    got = compute({"key": "v", "kind": "avg", "field": "measured_value"}, money)
    eq(got["value"], 2000.0, "an average divides by the rows that had a value")

    # A term and a field share one namespace on the form.
    eq(compute({"key": "v", "kind": "sum", "field": "retention_pct"},
               [rec(terms={"retention_pct": "10"})])["value"], 10.0,
       "a metric finds a key whether it is a field or a term")

    thin = money[:2]
    eq(compute({"key": "v", "kind": "avg", "field": "measured_value"}, thin)["value"], None,
       "an average over two rows is withheld")
    eq(compute({"key": "v", "kind": "sum", "field": "measured_value"}, thin)["value"], 3000.0,
       "but a total is still a total")
    eq(compute({"key": "v", "kind": "avg", "field": "measured_value"}, thin)["n"], 2,
       "and the sample size is reported even when the figure is not")

    spans = [rec(reached={"a": d0, "b": d0 + timedelta(days=2)}),
             rec(reached={"a": d0, "b": d0 + timedelta(days=4)}),
             rec(reached={"a": d0, "b": d0 + timedelta(days=6)}),
             rec(reached={"a": d0}),
             rec(reached={"b": d0})]
    got = compute({"key": "v", "kind": "days_between", "from": "a", "to": "b"}, spans)
    eq(got["value"], 4.0, "days between two stages is the mean of the completed ones")
    eq(got["n"], 3, "counting only records that reached both")
    eq(compute({"key": "v", "kind": "days_between", "from": "a", "to": "b"},
               spans[:2])["value"], None, "two spans are not enough to average")
    backwards = [rec(reached={"a": d0, "b": d0 - timedelta(days=1)})] * 3
    eq(compute({"key": "v", "kind": "days_between", "from": "a", "to": "b"},
               backwards)["n"], 0, "a stage reached before its predecessor is not a span")

    pool = [rec(reached={"returned": d0}), rec(), rec(), rec()]
    got = compute({"key": "v", "kind": "ratio", "of": {"stage_reached": "returned"},
                   "over": {"any": True}}, pool)
    eq(got["value"], 25.0, "a ratio is a percentage of the pool")
    eq(got["n"], 4, "over the whole pool")
    eq(compute({"key": "v", "kind": "ratio", "of": {"stage_reached": "returned"},
                "over": {"any": True}}, pool[:2])["value"], None,
       "a percentage of two records is withheld")

    marked = [rec(fields={"qc_result": "rejected"}), rec(fields={"qc_result": "accepted"}),
              rec(fields={"qc_result": "accepted"}), rec(fields={"qc_result": "accepted"})]
    eq(compute({"key": "v", "kind": "ratio", "of": {"field_equals": ["qc_result", "rejected"]},
                "over": {"any": True}}, marked)["value"], 25.0,
       "a ratio can test a field value")

    eq(compute({"key": "v", "kind": "count", "where": {"field_set": "cod_collected_on"}},
               [rec(fields={"cod_collected_on": "2026-06-01"}), rec()])["value"], 1,
       "a count counts what is filled in")
    eq(compute({"key": "v", "kind": "count", "where": {"any": True}}, [])["value"], 0,
       "and nothing counts as nothing")
    eq(compute({"key": "v", "kind": "nonsense"}, money)["value"], None,
       "an unknown shape computes nothing rather than guessing")

    # A report covers exactly the metrics the configuration declares.
    r = report("ecommerce", ["returns"], pool)
    eq(sorted(m["key"] for m in r), ["refund_value", "return_rate"],
       "the report carries this configuration's figures")
    eq(all(m["addon"] == "returns" for m in r), True, "each says which add-on it came from")
    eq(report("ecommerce", [], pool), [], "and nothing switched on measures nothing")

    print("template_addons.py: %d checks, %d failed" % (ok + len(bad), len(bad)))
    for line in bad:
        print("  FAIL " + line)
    return not bad


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(0 if self_test() else 1)
