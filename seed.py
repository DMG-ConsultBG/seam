# -*- coding: utf-8 -*-
"""
Demo seed - a high-demand business coordinating several recurring partner
relationships across different surfaces (general supply, 3PL fulfillment,
construction subcontracting). Shows that Seam is horizontal, not one vertical.

Descriptive demo text is stored as tokens ("§key") and localized by the backend
(see demo_i18n.py), so the showcase reads naturally in any UI language. Company
and people names plus pure codes/dates/amounts stay literal.

    py -3 seed.py            # create demo data (refuses if it already exists)
    py -3 seed.py --reset    # wipe the db file and recreate

Demo logins (password for all: demo1234):
    BUSINESS (pays, holds budget):  ops@seam.demo      Nordic Retail Group
    PARTNERS (free):                supply@seam.demo   Global Supply Co.
                                    fulfil@seam.demo   SpeedLogic 3PL
                                    build@seam.demo    BuildPro Contractors
"""
import os
import sys
import json
import datetime
import db
from werkzeug.security import generate_password_hash

PW = generate_password_hash("demo1234")


def reset_db():
    for ext in ("", "-wal", "-shm"):
        p = db.DB_PATH + ext
        if os.path.exists(p):
            os.remove(p)


def mk_user(c, email, name):
    return c.execute("INSERT INTO users (email,name,password_hash,verified) VALUES (?,?,?,1)", (email, name, PW)).lastrowid


def mk_org(c, name, kind):
    return c.execute("INSERT INTO orgs (name,kind) VALUES (?,?)", (name, kind)).lastrowid


def main():
    if "--reset" in sys.argv:
        reset_db()
    db.init_db()
    c = db.get_db()
    try:
        if c.execute("SELECT 1 FROM users WHERE email='ops@seam.demo'").fetchone():
            print("Demo data already exists. Run with --reset to recreate.")
            return

        # --- accounts -----------------------------------------------------
        biz_u = mk_user(c, "ops@seam.demo", "Maria Lindgren")
        biz_o = mk_org(c, "Nordic Retail Group", "business")
        c.execute("UPDATE orgs SET reg_number='BG205643632', country='BG', verified_company=1 WHERE id=?", (biz_o,))
        # Sectors are set further down, for every demo company at once: without
        # them the first sign-in opens the sector chooser, which is right for a
        # real company and wrong for a demo somebody is about to photograph.
        c.execute("UPDATE orgs SET contact_json=? WHERE id=?",
                  (json.dumps({"phone": "+359 2 400 1234", "whatsapp": "+359888123456",
                               "viber": "+359888123456", "telegram": "@nordicops",
                               "alt_email": "ops@nordic-retail.example",
                               "note": "§m_emg_note"}, ensure_ascii=False), biz_o))
        c.execute("INSERT INTO memberships (user_id,org_id,role) VALUES (?,?, 'owner')", (biz_u, biz_o))

        partners = {}
        for email, uname, oname in [
            ("supply@seam.demo", "Petra Novak", "Global Supply Co."),
            ("fulfil@seam.demo", "Stefan Berg", "SpeedLogic 3PL"),
            ("build@seam.demo", "David Marin", "BuildPro Contractors"),
        ]:
            u = mk_user(c, email, uname)
            o = mk_org(c, oname, "partner")
            c.execute("INSERT INTO memberships (user_id,org_id,role) VALUES (?,?, 'owner')", (u, o))
            partners[email] = (u, o, oname)

        # --- builders -----------------------------------------------------
        def mk_ws(name_token, template, partner_email):
            pu, po, pname = partners[partner_email]
            ws = c.execute(
                "INSERT INTO workspaces (name,template,buyer_org_id,partner_org_id,created_by) VALUES (?,?,?,?,?)",
                (name_token, template, biz_o, po, biz_u),
            ).lastrowid
            c.execute("INSERT INTO events (workspace_id,actor_user_id,actor_side,kind,summary,meta_json) VALUES (?,?,?,?,?,?)",
                      (ws, biz_u, "buyer", "workspace_created", "workspace_created",
                       json.dumps({"name": name_token}, ensure_ascii=False)))
            c.execute("INSERT INTO events (workspace_id,actor_user_id,actor_side,kind,summary,meta_json) VALUES (?,?,?,?,?,?)",
                      (ws, pu, "partner", "partner_joined", "partner_joined",
                       json.dumps({"name": pname}, ensure_ascii=False)))
            return ws, pu, po

        def stamp(days_ago):
            """A timestamp `days_ago` days back, in the format SQLite writes."""
            return (datetime.datetime.utcnow()
                    - datetime.timedelta(days=days_ago)).isoformat(" ", "seconds")

        def due(days):
            """A date `days` from today, negative for the past.

            Agreed dates used to be written as fixed literals, which meant that
            some months after this file was last touched every deadline in the
            demo was overdue and some fell before the record that carried them
            was opened. A deadline is only meaningful relative to now.
            """
            return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()

        def add_order(ws, tpl_key, pu, po, ref, title, status, fields, terms, events, comments,
                      notify=None, ago=0, quiet=0):
            # `ago` is how many days back the record was opened, `quiet` how
            # long ago the last thing happened to it. Everything in between is
            # spread evenly across that span, oldest first.
            #
            # Without this every event carried the same timestamp, which is why
            # the analytics screen showed an average cycle of nothing and a time
            # to agreement of zero days: the figures were right, the data simply
            # had no history to measure. A demo of a platform whose whole
            # subject is elapsed time cannot happen entirely in one instant.
            span = max(0.0, float(ago) - float(quiet))
            step = span / max(1, len(events) - 1) if len(events) > 1 else 0.0
            at = lambda i: max(0.0, ago - step * i)          # noqa: E731
            oid = c.execute(
                "INSERT INTO orders (workspace_id,ref,title,template,status,fields_json,created_by,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (ws, ref, title, tpl_key, status, json.dumps(fields, ensure_ascii=False), biz_u,
                 stamp(ago), stamp(at(len(events) - 1) if events else ago)),
            ).lastrowid
            when = {}
            for i, (_side, kind, meta) in enumerate(events):
                when.setdefault(kind, []).append(at(i))

            for k, (val, state, side) in terms.items():
                org = biz_o if side == "buyer" else po
                uid = biz_u if side == "buyer" else pu
                # An agreed term carries the day it was settled, exactly as it
                # would if the two sides had clicked accept - which is the day
                # the matching term_agreed event was written, not today.
                agreed = (when.get("term_agreed") or [None])[0]
                c.execute("INSERT INTO order_terms (order_id,key,value,state,proposed_by_org,updated_by,"
                          "updated_at,agreed_at) VALUES (?,?,?,?,?,?,?,?)",
                          (oid, k, val, state, org, uid,
                           stamp(agreed if agreed is not None else 0),
                           stamp(agreed if agreed is not None else 0) if state == "agreed" else None))
            for i, (side, kind, meta) in enumerate(events):
                uid = biz_u if side == "buyer" else pu
                m = dict(meta or {})
                if kind == "order_created":
                    m = {"ref": ref, "title": title}
                elif kind in ("status_changed", "term_proposed", "term_agreed", "field_updated"):
                    m.setdefault("tpl", tpl_key)
                c.execute("INSERT INTO events (workspace_id,order_id,actor_user_id,actor_side,kind,summary,"
                          "meta_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                          (ws, oid, uid, side, kind, kind, json.dumps(m, ensure_ascii=False),
                           stamp(at(i))))
            for (side, b) in comments:
                uid = biz_u if side == "buyer" else pu
                c.execute("INSERT INTO comments (order_id,user_id,body) VALUES (?,?,?)", (oid, uid, b))
            for (uid, kind, meta) in (notify or []):
                m = dict(meta or {}); m.setdefault("ref", ref)
                if kind in ("status_changed", "term_proposed", "term_agreed"):
                    m.setdefault("tpl", tpl_key)
                c.execute("INSERT INTO notifications (user_id,workspace_id,order_id,kind,body,meta_json) VALUES (?,?,?,?,?,?)",
                          (uid, ws, oid, kind, kind, json.dumps(m, ensure_ascii=False)))
            return oid

        # =================================================================
        #  1) General supply
        # =================================================================
        ws1, pu1, po1 = mk_ws("§ws_supply", "general", "supply@seam.demo")
        add_order(ws1, "general", pu1, po1, "REQ-0001", "§o_packaging", "in_progress",
                  {"summary": "§f_boxes", "reference": "PO-2241", "quantity": "§f_qty_boxes"},
                  {"price": ("EUR 1950", "agreed", "partner"),
                   "deadline": (due(11), "agreed", "buyer"),
                   "scope": ("§t_scope_dc", "agreed", "partner")},
                  [("buyer", "order_created", {}),
                   ("partner", "term_proposed", {"term": "price", "old": "", "new": "EUR 1950"}),
                   ("buyer", "term_agreed", {"term": "price", "value": "EUR 1950"}),
                   ("buyer", "status_changed", {"from": "requested", "to": "accepted"}),
                   ("partner", "status_changed", {"from": "accepted", "to": "in_progress"})],
                  [("buyer", "§c_friday"), ("partner", "§c_gradea")],
                  ago=26, quiet=3)

        add_order(ws1, "general", pu1, po1, "REQ-0002", "§o_pallets", "review",
                  {"summary": "§f_pallets", "reference": "PO-2248", "quantity": "§f_qty_pallets"},
                  {"price": ("EUR 2900", "proposed", "partner"),
                   "deadline": (due(18), "proposed", "partner")},
                  [("buyer", "order_created", {}),
                   ("partner", "term_proposed", {"term": "price", "old": "", "new": "EUR 2900"}),
                   ("buyer", "status_changed", {"from": "requested", "to": "review"})],
                  [("partner", "§c_gradea")],
                  notify=[(biz_u, "term_proposed", {"term": "price"})],
                  ago=9, quiet=1)

        # Two records that went the whole way. Without at least one of these the
        # analytics screen has no completion rate, no cycle time and nothing on
        # the closed side of its chart, which makes the product look like it
        # cannot measure the thing it exists to measure.
        add_order(ws1, "general", pu1, po1, "REQ-0003", "§o_shelving", "closed",
                  {"summary": "§f_shelving", "reference": "PO-2203",
                   "quantity": "§f_qty_shelving"},
                  {"price": ("EUR 4750", "agreed", "partner"),
                   # Met three days early, which is what makes the on-time
                   # figure on the reference card a measurement rather than
                   # a claim.
                   "deadline": (due(-35), "agreed", "buyer"),
                   "scope": ("§t_scope_dc", "agreed", "partner")},
                  [("buyer", "order_created", {}),
                   ("partner", "term_proposed", {"term": "price", "old": "", "new": "EUR 4750"}),
                   ("buyer", "term_agreed", {"term": "price", "value": "EUR 4750"}),
                   ("buyer", "status_changed", {"from": "requested", "to": "accepted"}),
                   ("partner", "status_changed", {"from": "accepted", "to": "in_progress"}),
                   ("partner", "status_changed", {"from": "in_progress", "to": "review"}),
                   ("buyer", "status_changed", {"from": "review", "to": "completed"}),
                   ("buyer", "status_changed", {"from": "completed", "to": "closed"})],
                  [("buyer", "§c_received_ok")],
                  ago=71, quiet=38)

        # workspace-level communication demo
        c.execute("INSERT INTO ws_messages (workspace_id,user_id,body) VALUES (?,?,?)", (ws1, biz_u, "§m_kickoff"))
        c.execute("INSERT INTO ws_messages (workspace_id,user_id,body) VALUES (?,?,?)", (ws1, pu1, "§m_reply"))

        # =================================================================
        #  2) E-commerce <-> 3PL fulfillment
        # =================================================================
        ws2, pu2, po2 = mk_ws("§ws_fulfil", "ecommerce", "fulfil@seam.demo")
        add_order(ws2, "ecommerce", pu2, po2, "FUL-0001", "§o_batch1", "shipped",
                  {"order_ref": "#4471", "units": "320", "destination": "§f_dest_eu"},
                  {"price": ("EUR 1100", "agreed", "partner"),
                   "sla": ("§t_sla_24", "agreed", "buyer")},
                  [("buyer", "order_created", {}),
                   ("partner", "status_changed", {"from": "received", "to": "picking"}),
                   ("partner", "status_changed", {"from": "picking", "to": "shipped"})],
                  [("partner", "§c_sla_ok")],
                  ago=17, quiet=2)

        add_order(ws2, "ecommerce", pu2, po2, "FUL-0002", "§o_returns", "exception",
                  {"order_ref": "RET-26", "units": "17", "notes": "§f_damaged"},
                  {"sla": ("§t_proc_48", "agreed", "buyer")},
                  [("buyer", "order_created", {}),
                   ("partner", "status_changed", {"from": "received", "to": "exception"})],
                  [("partner", "§c_damaged_q"), ("buyer", "§c_scrap")],
                  notify=[(biz_u, "status_changed", {"to": "exception"})],
                  ago=21, quiet=19)

        add_order(ws2, "ecommerce", pu2, po2, "FUL-0003", "§o_batch_w22", "closed",
                  {"order_ref": "#4318", "units": "410", "destination": "§f_dest_eu"},
                  {"price": ("EUR 1380", "agreed", "partner"),
                   "sla": ("§t_sla_24", "agreed", "buyer")},
                  [("buyer", "order_created", {}),
                   ("partner", "term_proposed", {"term": "price", "old": "", "new": "EUR 1380"}),
                   ("buyer", "term_agreed", {"term": "price", "value": "EUR 1380"}),
                   ("partner", "status_changed", {"from": "received", "to": "picking"}),
                   ("partner", "status_changed", {"from": "picking", "to": "shipped"}),
                   ("partner", "status_changed", {"from": "shipped", "to": "delivered"}),
                   ("buyer", "status_changed", {"from": "delivered", "to": "closed"})],
                  [("partner", "§c_delivered_ok")],
                  ago=52, quiet=31)

        # =================================================================
        #  3) Construction <-> subcontractor
        # =================================================================
        ws3, pu3, po3 = mk_ws("§ws_build", "construction", "build@seam.demo")
        so1 = add_order(ws3, "construction", pu3, po3, "SUB-0001", "§o_counter", "in_progress",
                  {"work_item": "§f_counter", "location": "§f_loc_mall"},
                  {"price": ("EUR 12400", "agreed", "partner"),
                   "milestone_date": (due(27), "agreed", "buyer"),
                   "payment_terms": ("§t_pay_5050", "agreed", "buyer"),
                   "scope": ("§t_scope_full", "agreed", "partner")},
                  [("buyer", "order_created", {}),
                   ("partner", "term_proposed", {"term": "price", "old": "", "new": "EUR 12400"}),
                   ("buyer", "term_agreed", {"term": "price", "value": "EUR 12400"}),
                   ("buyer", "status_changed", {"from": "quoted", "to": "in_progress"})],
                  [("buyer", "§c_night")],
                  ago=34, quiet=4)

        add_order(ws3, "construction", pu3, po3, "SUB-0002", "§o_electrical", "quoted",
                  {"work_item": "§f_electrical", "location": "§f_loc_store_b",
                   "spec": "§f_spec_electrical"},
                  {"price": ("EUR 8900", "proposed", "partner"),
                   "milestone_date": (due(46), "proposed", "partner")},
                  [("buyer", "order_created", {}),
                   ("partner", "term_proposed", {"term": "price", "old": "", "new": "EUR 8900"})],
                  [("partner", "§c_cert")],
                  notify=[(biz_u, "term_proposed", {"term": "price"})],
                  ago=6, quiet=1)

        # -----------------------------------------------------------------
        #  Progress-evidence demo: two "site photos" on SUB-0001, uploaded
        #  by the subcontractor (documented proof of a completed stage).
        # -----------------------------------------------------------------
        up_dir = os.path.join(os.path.dirname(__file__), "uploads", str(so1))
        os.makedirs(up_dir, exist_ok=True)
        photos = [
            ("a1b2c3d4e5f60718.svg", "site-photo-01.svg",
             "<svg xmlns='http://www.w3.org/2000/svg' width='800' height='600'>"
             "<defs><linearGradient id='s' x1='0' y1='0' x2='0' y2='1'>"
             "<stop offset='0' stop-color='#8fb0d4'/><stop offset='.62' stop-color='#d9c9a8'/></linearGradient></defs>"
             "<rect width='800' height='600' fill='url(#s)'/>"
             "<rect x='70' y='250' width='300' height='290' fill='#8a6d4a'/>"
             "<rect x='110' y='300' width='90' height='110' fill='#c9dae8'/>"
             "<rect x='410' y='320' width='320' height='220' fill='#6f7d8c'/>"
             "<rect x='450' y='360' width='240' height='30' fill='#f5b940'/>"
             "<circle cx='660' cy='110' r='55' fill='#f7de8b' opacity='.9'/></svg>"),
            ("b2c3d4e5f6071829.svg", "site-photo-02.svg",
             "<svg xmlns='http://www.w3.org/2000/svg' width='800' height='600'>"
             "<defs><linearGradient id='s2' x1='0' y1='0' x2='0' y2='1'>"
             "<stop offset='0' stop-color='#5d6f85'/><stop offset='.7' stop-color='#3f4a59'/></linearGradient></defs>"
             "<rect width='800' height='600' fill='url(#s2)'/>"
             "<rect x='120' y='180' width='560' height='340' rx='10' fill='#7d6b52'/>"
             "<rect x='160' y='220' width='480' height='40' fill='#f5d76e'/>"
             "<rect x='160' y='290' width='220' height='190' fill='#a8b7c6'/>"
             "<rect x='420' y='290' width='220' height='190' fill='#93a5b8'/></svg>"),
        ]
        for fname, orig, svg in photos:
            with open(os.path.join(up_dir, fname), "w", encoding="utf-8") as f:
                f.write(svg)
            c.execute("INSERT INTO attachments (order_id,user_id,filename,original_name,kind,size) VALUES (?,?,?,?, 'image', ?)",
                      (so1, pu3, fname, orig, len(svg)))
        c.execute("INSERT INTO events (workspace_id,order_id,actor_user_id,actor_side,kind,summary,meta_json) VALUES (?,?,?,?,?,?,?)",
                  (ws3, so1, pu3, "partner", "attachment_added", "attachment_added",
                   json.dumps({"name": "site-photo-01.svg, site-photo-02.svg", "count": 2}, ensure_ascii=False)))

        # =================================================================
        #  Store demo (physical stock + digital key vault)
        # =================================================================
        def _img(bg, fg, ico):
            svg = ("<svg xmlns='http://www.w3.org/2000/svg' width='240' height='160'>"
                   "<rect width='240' height='160' fill='%s'/>"
                   "<text x='120' y='104' font-size='72' text-anchor='middle'>%s</text></svg>" % (bg, ico))
            import base64
            return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode()

        pc = c.execute("INSERT INTO products (org_id,name,type,sku,price,stock,image) VALUES (?,?,?,?,?,?,?)",
                       (biz_o, "§p_cable", "physical", "CBL-2M", "EUR 12", 40, _img("#e7eefb", "#000", "🔌"))).lastrowid
        pl = c.execute("INSERT INTO products (org_id,name,type,sku,price,stock,image) VALUES (?,?,?,?,?,?,?)",
                       (biz_o, "§p_license", "digital", "LIC-PRO", "EUR 49", 0, _img("#eee9fb", "#000", "🗝️"))).lastrowid
        pg = c.execute("INSERT INTO products (org_id,name,type,sku,price,stock,image) VALUES (?,?,?,?,?,?,?)",
                       (biz_o, "§p_gamekey", "digital", "GAME", "EUR 25", 0, _img("#e6f6ef", "#000", "🎮"))).lastrowid
        for code in ["LIC-PRO-1A2B-3C4D", "LIC-PRO-5E6F-7G8H", "LIC-PRO-9I0J-1K2L", "LIC-PRO-3M4N-5O6P", "LIC-PRO-7Q8R-9S0T"]:
            c.execute("INSERT INTO product_keys (product_id,code) VALUES (?,?)", (pl, code))
        for code in ["GAME-AAAA-1111", "GAME-BBBB-2222"]:
            c.execute("INSERT INTO product_keys (product_id,code) VALUES (?,?)", (pg, code))
        c.execute("INSERT INTO store_orders (org_id,ref,customer,product_id,qty,status,source) VALUES (?,?,?,?,?, 'new', 'manual')",
                  (biz_o, "SO-0001", "§c_client_a", pc, 2))
        c.execute("INSERT INTO store_orders (org_id,ref,customer,product_id,qty,status,source) VALUES (?,?,?,?,?, 'new', 'form')",
                  (biz_o, "SO-0002", "§c_client_b", pl, 1))
        so3 = c.execute("INSERT INTO store_orders (org_id,ref,customer,product_id,qty,status,source,fulfilled_at) VALUES (?,?,?,?,?, 'fulfilled', 'api', datetime('now'))",
                        (biz_o, "SO-0003", "§c_client_b", pl, 1)).lastrowid

        # Inbound integration demo keys (webhook secret + public order-form link)
        c.execute("INSERT INTO api_keys (org_id,key,label,kind) VALUES (?,?,?,?)",
                  (biz_o, "sk_seam_demo_Xq3vRr8pTz1LmN5c", "Web", "secret"))
        c.execute("INSERT INTO api_keys (org_id,key,label,kind) VALUES (?,?,?,?)",
                  (biz_o, "pk_seam_demo_Fh7wKd2sYb9GtQ4e", "Web", "public"))
        k = c.execute("SELECT id FROM product_keys WHERE product_id=? AND status='available' ORDER BY id LIMIT 1", (pl,)).fetchone()
        if k:
            c.execute("UPDATE product_keys SET status='delivered', order_id=? WHERE id=?", (so3, k["id"]))

        # =================================================================
        #  5) Money: invoices in both directions
        # =================================================================
        # Without these the Money screen opens empty, on three zeroes and two
        # warnings, which is the one screen where the product has to look like
        # it has been used. The subcontractors invoice the retailer, so the
        # retailer sees what it owes and each subcontractor sees what it is
        # owed and what has gone past its due date.
        import secrets as _secrets
        import receivables

        TODAY = datetime.date.today()

        def day(n):
            return (TODAY + datetime.timedelta(days=n)).isoformat()

        # The sectors go on every demo company, not only the retailer. Setting
        # them on one left each partner account opening on the sector chooser,
        # which then sat in the middle of the receivables photograph.
        # A country on every company, not only the retailer: without one the
        # finance screen has no VAT rate and no profit tax to work with, and a
        # partner would be shown a profit equal to its revenue.
        for org_id, country in ((biz_o, "BG"), (po1, "BG"), (po2, "NL"), (po3, "BG")):
            c.execute("UPDATE orgs SET country=? WHERE id=?", (country, org_id))

        # What it costs BuildPro to be BuildPro. Without these the profit
        # figure is the revenue figure wearing a different label.
        for kind, label, amount, vat, recurring, on_day in (
                # Monthly figures, and a recurring cost is counted in every
                # month of the period asked for. Scaled to the size of this
                # demo: a crew whose wage bill is four times its revenue is not
                # a demonstration of anything.
                ("lease", "Наем на база и склад", 450.0, 90.0, 1, None),
                ("lease", "Лизинг, бордови автомобил", 260.0, 52.0, 1, None),
                ("salary", "Работни заплати", 700.0, 0.0, 1, None),
                ("operating", "Гориво и пътни", 180.0, 36.0, 1, None),
                ("operating", "Софтуер и телефони", 40.0, 8.0, 1, None),
                ("other", "Застраховка, годишна вноска", 400.0, 0.0, 0,
                 (TODAY - datetime.timedelta(days=40)).isoformat())):
            c.execute(
                "INSERT INTO expenses (org_id, kind, label, amount, currency, vat_amount, "
                "recurring, on_date, starts_on, created_by) VALUES (?,?,?,?, 'EUR', ?,?,?,?,?)",
                (po3, kind, label, amount, vat, recurring, on_day,
                 (TODAY - datetime.timedelta(days=365)).isoformat() if recurring else None,
                 pu3))

        for org_id, iban, bic, bank, sectors in (
                (biz_o, "BG18RZBB91550123456789", "RZBBBGSF", "Райфайзенбанк",
                 ["general", "ecommerce", "construction", "auto-import"]),
                (po1, "BG72UNCR70001523456789", "UNCRBGSF", "УниКредит Булбанк", ["general"]),
                (po2, "NL91ABNA0417164300", "ABNANL2A", "ABN AMRO", ["ecommerce"]),
                (po3, "BG40FINV91501012345678", "FINVBGSF", "Първа инвестиционна банка",
                 ["construction"])):
            c.execute("UPDATE orgs SET pay_iban=?, pay_bic=?, pay_bank=?, pay_terms=30, "
                      "sectors_json=? WHERE id=?",
                      (iban, bic, bank, json.dumps(sectors), org_id))

        # The retailer and its building subcontractor are on a plan; the other
        # two partners are not. A demo where everyone is on the free tier puts a
        # "buy this part" panel over half the product, so the screen that shows
        # per-country tax and filing shows a paywall instead of a calculation.
        # Leaving two companies unsubscribed keeps the other side of the gate
        # visible, which is the half that has to be believable too.
        #
        # This is the instance operator seeding its own showcase, not a company
        # claiming to have paid: the rule that a plan is only ever opened by a
        # signed webhook, a processor answer or a recorded transfer governs the
        # running application, and is not weakened by what a local script writes
        # into a demo database it just created.
        for org_id in (biz_o, po3):
            c.execute("UPDATE orgs SET plan='year', plan_until=? WHERE id=?",
                      (due(300), org_id))

        # --- published directory profiles ---------------------------------
        # Without these the directory is an empty page with a filter bar on
        # top, which is exactly what the feature does not do. Every company
        # here has the four things profiles.may_list insists on, so each one
        # reaches the "listed" tier honestly rather than by setting the flag.
        #
        # The text is English, like the company names it belongs to: these are
        # the fictional companies' own words, not interface strings, and the
        # chrome around them still comes up in whichever language is chosen.
        for org_id, posture, headline, about, size, founded, areas, email in (
                (biz_o, "seeking",
                 "Retail chain, 40 stores across Bulgaria and Romania",
                 "We run forty stores and buy shop fit-outs, logistics and "
                 "seasonal supply on framework agreements. We pay on thirty "
                 "days and expect a delivery note against every order.",
                 "large", "2009", ["BG", "RO"], "ops@nordic-retail.example"),
                (po1, "offering",
                 "Wholesale packaging and shop consumables",
                 "Pallets, shelving consumables and packaging from stock, with "
                 "next-day delivery inside Bulgaria. Thirty years of supplying "
                 "retail chains, and a standing line for urgent replacements.",
                 "medium", "1994", ["BG"], "sales@global-supply.example"),
                (po2, "offering",
                 "Third-party fulfilment for online stores",
                 "We pick, pack and ship for online retailers from two "
                 "warehouses in the Netherlands. Same-day dispatch on orders "
                 "placed before four, and returns handled end to end.",
                 "medium", "2016", ["NL", "BE", "DE"], "hello@speedlogic.example"),
                (po3, "both",
                 "Electrical and fit-out works for commercial sites",
                 "A crew of eighteen doing electrical installation, lighting "
                 "and interior fit-out for shops and offices. Licensed, "
                 "insured, and used to working nights inside a trading store.",
                 "small", "2011", ["BG"], "office@buildpro.example")):
            c.execute("UPDATE orgs SET posture=?, headline=?, about=?, size_band=?, "
                      "founded=?, areas_json=?, contact_email=?, listed=1 WHERE id=?",
                      (posture, headline, about, size, founded,
                       json.dumps(areas), email, org_id))

        # Past work, so a card says what the company has actually done rather
        # than "0 entries". Value bands, never figures: reference.py explains
        # why a portfolio may not become a price list.
        for org_id, kind, title, summary, role, place, year, band in (
                (po1, "project", "Packaging line for a 40-store chain",
                 "Standing supply of pallets, stretch film and shelf "
                 "consumables, replenished weekly against a rolling forecast.",
                 "Supplier", "Sofia", "2024", "50-100k"),
                (po1, "certificate", "ISO 9001:2015",
                 "Quality management, audited annually.", "Certificate holder",
                 "Sofia", "2023", None),
                (po1, "equipment", "Two 12t trucks and a 3,000 m2 warehouse",
                 "Own fleet, so a replacement pallet is on site the same day.",
                 "Owner", "Sofia", "2022", None),
                (po2, "project", "Fulfilment for a Dutch fashion retailer",
                 "Pick, pack and ship for 1,200 orders a day in season, with "
                 "returns processed inside 24 hours.",
                 "3PL provider", "Rotterdam", "2025", "100-250k"),
                (po2, "project", "Cross-border dispatch to DE and BE",
                 "Two-warehouse split so an order ships from whichever side of "
                 "the border is closer to the customer.",
                 "3PL provider", "Venlo", "2024", "50-100k"),
                (po2, "certificate", "GDP warehouse certification",
                 "Good distribution practice, for temperature-controlled lines.",
                 "Certificate holder", "Venlo", "2024", None),
                (po3, "project", "Full electrical fit-out, 900 m2 store",
                 "Distribution boards, lighting and data cabling, done in "
                 "night shifts while the store stayed open.",
                 "Main electrical contractor", "Plovdiv", "2025", "25-50k"),
                (po3, "licence", "Part P electrical licence",
                 "Registered for commercial installation work.",
                 "Licence holder", "Plovdiv", "2019", None),
                (po3, "project", "Lighting refit across six branches",
                 "LED conversion with the measured saving handed over on paper.",
                 "Subcontractor", "Burgas", "2024", "10-25k"),
                (biz_o, "project", "Framework buying for 40 stores",
                 "Fit-out, logistics and seasonal supply bought on frameworks "
                 "rather than one purchase order at a time.",
                 "Buyer", "Sofia", "2025", "250k+"),
                (biz_o, "award", "Retailer of the year, regional",
                 "Awarded for the 2024 store programme.", "Recipient",
                 "Sofia", "2024", None)):
            # Filed by that company's own owner, not by the retailer: an entry
            # created_by somebody outside the org is a row no screen could
            # have produced.
            c.execute(
                "INSERT INTO portfolio (org_id, kind, title, summary, role, place, "
                "year, value_band, created_by) VALUES (?,?,?,?,?,?,?,?,?)",
                (org_id, kind, title, summary, role, place, year, band,
                 {biz_o: biz_u, po1: pu1, po2: pu2, po3: pu3}[org_id]))

        def invoice(seller, ws, order, number, net, issued, due, status,
                    paid_on=None, reminders=0, lines=()):
            vat = round(net * 0.20, 2)
            gross = round(net + vat, 2)
            c.execute(
                "INSERT INTO invoices (org_id, customer_org_id, workspace_id, order_id, number, "
                "pay_ref, kind, customer, customer_reg, customer_vat, customer_email, lang, "
                "currency, net, vat_rate, vat_amount, gross, paid_amount, issue_date, tax_date, "
                "due_date, status, sent_at, paid_at, reminders, lines_json, created_by) "
                "VALUES (?,?,?,?,?,?,'invoice',?,?,?,?,'bg','EUR',?,20,?,?,?,?,?,?,?,?,?,?,?,?)",
                (seller, biz_o, ws, order, number,
                 receivables.make_ref(number, _secrets.token_bytes(8)),
                 "Nordic Retail Group", "BG205643632", "BG205643632", "ops@seam.demo",
                 net, vat, gross, gross if status == "paid" else 0,
                 day(issued), day(issued), day(due), status,
                 day(issued), day(paid_on) if paid_on is not None else None, reminders,
                 json.dumps([{"desc": d, "qty": q, "price": p} for d, q, p in lines],
                            ensure_ascii=False),
                 biz_u))

        # The line text is literal, not a §token: an invoice is issued in one
        # language and stays in it. A document that changed language depending
        # on who opened it would not be the document that was sent.
        invoice(po3, ws3, so1, "INV-2026-0028", 12400.0, -70, -40, "paid", paid_on=-38,
                lines=(("Разбиване и извозване", 1, 4200.0),
                       ("Зидария и мазилки", 1, 8200.0)))
        invoice(po3, ws3, so1, "INV-2026-0031", 8900.0, -46, -16, "sent", reminders=2,
                lines=(("Подмяна на ел. табло + окабеляване", 1, 8900.0),))
        # Due dates are written at seed time, as a real invoice's are, so the
        # demo ages: the one invoice meant to read as "open, not yet due"
        # turned overdue eighteen days later and both money tiles showed the
        # same number. Widened so a seeded demo stays truthful for a quarter
        # rather than a fortnight.
        invoice(po3, ws3, None, "INV-2026-0034", 3250.0, -12, 76, "sent",
                lines=(("Довършителни работи, Магазин В", 1, 3250.0),))
        invoice(po1, ws1, None, "INV-1174", 4150.0, -55, -25, "paid", paid_on=-27,
                lines=(("EUR палети 1200x800", 200, 20.75),))
        invoice(po1, ws1, None, "INV-1188", 5600.0, -20, 68, "sent",
                lines=(("EUR палети 1200x800", 280, 20.0),))

        c.commit()
        print("[OK] Demo data created (multi-vertical, localized tokens).")
        print("  BUSINESS: ops@seam.demo / demo1234")
        print("  PARTNERS: supply@seam.demo | fulfil@seam.demo | build@seam.demo  (/ demo1234)")
    finally:
        c.close()


if __name__ == "__main__":
    main()
