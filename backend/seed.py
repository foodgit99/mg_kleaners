"""Starter playbook + clearly-labelled DEMO data (source='demo').
Demo customers are fictional. Offers are starter ideas for the MG Kleaners team
to edit: the agent only proposes offers that exist in this playbook."""
import random
from datetime import date, timedelta

STARTER_OFFERS = [
    ("PRIORITY_SLOT", "Priority slot", "We can hold your preferred day and time this week.",
     "due_reminder,check_in,welcome_back"),
    ("FIX_IT_FREE", "We'll make it right", "If anything wasn't right last time, we'll come back and fix it at no cost.",
     "check_in,thank_you"),
    ("FREE_ADDON", "Free add-on", "Your next deep clean comes with a free fridge or oven clean.",
     "win_back,loyalty_reward,check_in"),
    ("BUNDLE_FUMIGATION", "Clean + fumigate bundle", "Add fumigation to your next clean and save on the combined visit.",
     "due_reminder,loyalty_reward"),
    ("COMEBACK_10", "Welcome-back 10%", "Enjoy 10% off your first booking back with us.", "win_back"),
    ("REFER_A_FRIEND", "Refer a friend", "Refer a friend and you both get a free add-on on your next visit.",
     "thank_you,loyalty_reward,welcome_back"),
]


def seed_offers(c):
    for code, title, desc, fits in STARTER_OFFERS:
        c.execute("""INSERT OR IGNORE INTO offers(code,title,description,fits,created_by,approved_by)
                     VALUES(?,?,?,?,?,?)""", (code, title, desc, fits, "starter", "starter"))


SERVICES = {
    "Home Deep Cleaning": (35000, 60000), "Regular Home Cleaning": (15000, 25000),
    "Office Cleaning": (40000, 90000), "Fumigation": (25000, 45000),
    "Upholstery & Rug Cleaning": (20000, 50000), "Post-Construction Cleaning": (120000, 250000),
}
AREAS = ["Rayfield", "Bukuru", "Tudun Wada", "Lamingo", "Angwan Rukuba", "Old Airport", "Dadin Kowa", "Terminus",
         "Jenta Adamu", "Farin Gada"]

# name, kind, main service, cadence days, visits, days since last visit, extra
DEMO = [
    ("Mrs. Ngozi Okafor", "home", "Home Deep Cleaning", 30, 9, 33, "Prefers Saturday mornings"),
    ("Dr. Musa Dalyop", "home", "Regular Home Cleaning", 14, 14, 9, None),
    ("Grace Pam", "home", "Home Deep Cleaning", 30, 5, 58, "Has a newborn — asked for fragrance-free products"),
    ("Tukur & Sons Pharmacy", "business", "Office Cleaning", 7, 30, 6, None),
    ("Hajia Amina Bello", "home", "Fumigation", 90, 4, 150, None),
    ("Emeka Nwosu", "home", "Upholstery & Rug Cleaning", 120, 3, 410, None),
    ("Plateau Hills Hotel", "business", "Office Cleaning", 7, 26, 24, "Complained about late arrival in August"),
    ("Pst. John Gyang", "home", "Home Deep Cleaning", 45, 6, 40, None),
    ("Blessing Danjuma", "home", "Regular Home Cleaning", 14, 8, 30, None),
    ("Kemi Adeyemi", "home", "Home Deep Cleaning", 30, 1, 12, None),
    ("Jos Tech Hub", "business", "Office Cleaning", 14, 12, 15, "Pays via transfer; contact is the admin, Ruth"),
    ("Mr. Ibrahim Lawal", "home", "Fumigation", 90, 3, 50, None),
    ("Sarah Choji", "home", "Post-Construction Cleaning", 365, 1, 70, "Just moved into a new duplex in Lamingo"),
    ("Engr. Paul Davou", "home", "Home Deep Cleaning", 30, 4, 140, None),
    ("Faith Bot", "home", "Regular Home Cleaning", 14, 10, 6, None),
    ("Mama Chi Kitchen", "business", "Fumigation", 60, 5, 62, "Restaurant — needs work done after closing hours"),
    ("Chief Bitrus Lar", "home", "Home Deep Cleaning", 30, 11, 26, "VIP — always tips the crew"),
    ("Aisha Yakubu", "home", "Upholstery & Rug Cleaning", 120, 2, 60, None),
    ("Rhoda Pwajok", "home", "Home Deep Cleaning", 60, 3, 230, None),
    ("Sunrise Montessori School", "business", "Office Cleaning", 30, 7, 35, "Term breaks are the best time for deep cleans"),
    ("Daniel Mancha", "home", "Regular Home Cleaning", 14, 1, 5, None),
    ("Comfort Ishaku", "home", "Fumigation", 90, 2, 40, None),
]


def seed_demo(c):
    rnd = random.Random(42)
    t = date.today()
    seed_offers(c)
    for idx, (name, kind, svc, cad, visits, since, note) in enumerate(DEMO):
        phone = "080" + str(30000000 + idx * 1371919)[:8]
        cur = c.execute("""INSERT INTO customers(name, phone, area, kind, source, channel_pref)
                           VALUES(?,?,?,?,?,?)""",
                        (name, phone, rnd.choice(AREAS), kind, "demo", "whatsapp" if idx % 4 else "call"))
        cid = cur.lastrowid
        d = t - timedelta(days=since)
        for v in range(visits):
            s = svc
            if v % 4 == 2 and kind == "home":
                s = rnd.choice(["Fumigation", "Upholstery & Rug Cleaning"])
            lo, hi = SERVICES[s]
            amt = round(rnd.randint(lo, hi), -3)
            c.execute("""INSERT INTO transactions(customer_id,date,service,amount,source,external_ref)
                         VALUES(?,?,?,?,?,?)""", (cid, d.isoformat(), s, amt, "demo", f"demo:{cid}:{v}"))
            d = d - timedelta(days=max(3, int(cad * rnd.uniform(0.8, 1.2))))
        if note:
            kind_m = "complaint" if "omplain" in note else ("preference" if any(w in note.lower() for w in
                                                            ("prefer", "fragrance", "after closing", "best time")) else "note")
            c.execute("INSERT INTO memory(customer_id,kind,content,author) VALUES(?,?,?,?)", (cid, kind_m, note, "demo"))

    # a little history so the Learning view has signal
    hist = [("PRIORITY_SLOT", 14, 6), ("FIX_IT_FREE", 9, 5), ("FREE_ADDON", 11, 4),
            ("BUNDLE_FUMIGATION", 6, 2), ("COMEBACK_10", 8, 2), ("REFER_A_FRIEND", 5, 1)]
    reasons = {"PRIORITY_SLOT": "due_reminder", "FIX_IT_FREE": "check_in", "FREE_ADDON": "win_back",
               "BUNDLE_FUMIGATION": "due_reminder", "COMEBACK_10": "win_back", "REFER_A_FRIEND": "thank_you"}
    ids = [r["id"] for r in c.execute("SELECT id FROM customers")]
    for code, sent, won in hist:
        for k in range(sent):
            cid = rnd.choice(ids)
            when = (t - timedelta(days=rnd.randint(35, 200))).isoformat()
            status = "rebooked" if k < won else rnd.choice(["no_response", "no_response", "replied", "declined"])
            c.execute("""INSERT INTO outreach(customer_id,reason,channel,message,offer_code,status,created_by,sent_at,created_at,updated_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?)""",
                      (cid, reasons[code], "whatsapp", "(demo history message)", code, status, "agent", when, when, when))
        c.execute("UPDATE offers SET sent=?, won=? WHERE code=?", (sent, won, code))
