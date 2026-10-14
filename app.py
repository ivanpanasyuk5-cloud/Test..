"""
EU-CIS Auction Hub — управление аукционными сделками (EVO SYSTEMS s.r.o.)
Запуск:  pip install -r requirements.txt  &&  streamlit run app.py

Переменные окружения (необязательно):
  EVO_DATA_DIR   — папка для данных (по умолчанию ./data)
  ADMIN_PIN      — PIN для роли «Администратор» (или st.secrets["ADMIN_PIN"])
"""

import html
import json
import os
import re
import shutil
import tempfile
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

# ----------------------------------------------------------------------------
# Конфигурация
# ----------------------------------------------------------------------------
st.set_page_config(
    page_title="EU-CIS Auction Hub",
    page_icon="🔨",
    layout="wide",
    initial_sidebar_state="auto",
)

DATA_DIR = Path(os.environ.get("EVO_DATA_DIR", "data"))
DEALS_FILE = DATA_DIR / "deals.json"
FILES_DIR = DATA_DIR / "files"
DATA_DIR.mkdir(parents=True, exist_ok=True)
FILES_DIR.mkdir(parents=True, exist_ok=True)

# Менеджер может скачивать документы? (по умолчанию — нет, только видит список)
MANAGER_CAN_DOWNLOAD = False

ROLE_ADMIN = "Администратор / Партнер"
ROLE_MANAGER = "Менеджер (Ограниченный)"

# 4 жёстко зашитые подпапки: (имя на диске, подпись)
SUBFOLDERS = [
    ("Инвойсы", "📁 Инвойсы"),
    ("Документы клиента", "📁 Документы клиента"),
    ("Подписанные договоры", "📁 Подписанные договоры"),
    ("Логистика", "📁 Логистика"),
]

STATUSES = {
    "active": ("🟢 Активна", "#16a34a"),
    "transit": ("🔵 В пути в СНГ", "#2563eb"),
    "closed": ("⚪ Завершена / Закрыта", "#6b7280"),
    "cancelled": ("🔴 Отменена", "#dc2626"),
}
ACTIVE_STAGES = ["Сбор средств", "Торги", "Выиграно"]

CSS = """
<style>
.badge{display:inline-block;padding:2px 11px;border-radius:999px;color:#fff;
       font-size:.82rem;font-weight:600;white-space:nowrap;vertical-align:middle}
.pinned{border-left:5px solid #f59e0b;background:rgba(245,158,11,.12);
        padding:.75rem 1rem;border-radius:8px;margin:.5rem 0 1rem 0;line-height:1.55}
.pinned b{font-size:.95rem}
.overdue{color:#dc2626;font-weight:600}
@media (max-width: 640px){
  .block-container{padding:1rem .7rem 3rem .7rem}
  .stButton>button,.stDownloadButton>button{width:100%}
}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# ----------------------------------------------------------------------------
# Хранилище
# ----------------------------------------------------------------------------
def load_deals() -> list:
    if not DEALS_FILE.exists():
        return []
    try:
        return json.loads(DEALS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_deals(deals: list) -> None:
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(deals, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DEALS_FILE)


def safe_name(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(name)).strip(" .")
    return cleaned or "x"


def next_deal_id(deals: list) -> str:
    nums = []
    for d in deals:
        m = re.search(r"(\d+)$", d.get("id", ""))
        if m:
            nums.append(int(m.group(1)))
    return f"DEAL-{(max(nums) + 1) if nums else 1001}"


def deal_root(deal: dict) -> Path:
    return FILES_DIR / deal["folder"]


def ensure_folders(deal: dict) -> None:
    for disk, _ in SUBFOLDERS:
        (deal_root(deal) / disk).mkdir(parents=True, exist_ok=True)


def find_deal(deals: list, deal_id: str):
    for d in deals:
        if d["id"] == deal_id:
            return d
    return None


# ----------------------------------------------------------------------------
# Хелперы отображения
# ----------------------------------------------------------------------------
def eur(x: float) -> str:
    return f"€{x:,.2f}".replace(",", " ")


def status_label(deal: dict) -> str:
    label, _ = STATUSES[deal["status"]]
    if deal["status"] == "active" and deal.get("stage"):
        label += f" · {deal['stage']}"
    return label


def badge(deal: dict) -> str:
    _, color = STATUSES[deal["status"]]
    return f'<span class="badge" style="background:{color}">{html.escape(status_label(deal))}</span>'


def parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except Exception:
        return date.today()


def is_overdue(deal: dict) -> bool:
    return deal["status"] == "active" and parse_date(deal["deadline"]) < date.today()


def contact_html(deal: dict) -> str:
    def esc(v):
        return html.escape(v or "").strip()

    phone = esc(deal.get("phone"))
    tg = esc(deal.get("telegram"))
    mail = esc(deal.get("email"))
    req = esc(deal.get("requisites")).replace("\n", "<br>")

    phone_h = f'<a href="tel:{re.sub(r"[^+0-9]", "", phone)}">{phone}</a>' if phone else "—"
    if tg:
        handle = tg.lstrip("@")
        tg_h = f'<a href="https://t.me/{handle}" target="_blank">@{handle}</a>'
    else:
        tg_h = "—"
    mail_h = f'<a href="mailto:{mail}">{mail}</a>' if mail else "—"
    return (
        '<div class="pinned"><b>📌 Контакты получателя</b><br>'
        f"📞 {phone_h} &nbsp;|&nbsp; ✈️ {tg_h} &nbsp;|&nbsp; ✉️ {mail_h}<br>"
        f"🏦 <b>Реквизиты:</b> {req or '—'}</div>"
    )


# ----------------------------------------------------------------------------
# Калькулятор
# ----------------------------------------------------------------------------
def service_commission(hammer: float):
    """Комиссия сервиса за выкуп (Прайс v2). Возвращает (сумма, описание)."""
    if hammer <= 0:
        return 0.0, "—"
    if hammer < 1000:
        return 100.0, "до €1 000: фикс. €100"
    if hammer < 5000:
        rate, mn, txt = 0.10, 150, "€1 000–5 000: 10% (мин. €150)"
    elif hammer < 20000:
        rate, mn, txt = 0.075, 500, "€5 000–20 000: 7.5% (мин. €500)"
    elif hammer < 50000:
        rate, mn, txt = 0.05, 1400, "€20 000–50 000: 5% (мин. €1 400)"
    else:
        rate, mn, txt = 0.035, 2500, "от €50 000: 3.5% (мин. €2 500)"
    return max(hammer * rate, float(mn)), txt


EXTRA_SERVICES = [
    ("Индивидуальный подбор лота под ТЗ", 100.0),
    ("Базовый осмотр лота", 200.0),
    ("Экспертная инспекция спецоборудования", 450.0),
    ("Демонтаж и разборка габаритного груза / станков", 350.0),
    ("Оформление экспортных документов EX-1", 100.0),
]


def render_calculator(deals: list, is_admin: bool):
    st.subheader("🧮 Смета «под ключ» (прайс EVO SYSTEMS s.r.o., v2)")

    options = ["— ввести вручную —"] + [f"{d['id']} · {d['client']} · {d['lot']}" for d in deals]
    choice = st.selectbox("Подтянуть данные из сделки", options, key="calc_pick")
    sel = deals[options.index(choice) - 1] if choice != options[0] else None
    k = sel["id"] if sel else "manual"

    # --- Лот и сбор аукциона
    hammer = st.number_input(
        "Цена лота (Hammer Price), €", min_value=0.0, step=100.0,
        value=float(sel["hammer"]) if sel else 0.0, key=f"c_h_{k}",
    )
    pm = st.radio("Сбор аукциона (Buyer's Premium) задать как", ["Сумма, €", "% от ставки"],
                  horizontal=True, key=f"c_pm_{k}")
    if pm == "Сумма, €":
        premium = st.number_input(
            "Сбор аукциона, €", min_value=0.0, step=10.0,
            value=float(sel["premium"]) if sel else 0.0, key=f"c_p_{k}",
        )
    else:
        pct = st.number_input("Сбор аукциона, % от ставки", min_value=0.0, step=0.5,
                              value=20.0, key=f"c_pp_{k}")
        premium = hammer * pct / 100.0
        st.caption(f"= {eur(premium)}")

    # --- Комиссия сервиса
    commission, tier = service_commission(hammer)
    st.info(f"Комиссия сервиса: **{eur(commission)}** ({tier})")

    # --- Доп. услуги
    st.markdown("**Дополнительные услуги** (отметьте нужные, цену можно изменить)")
    extras_total = 0.0
    chosen = []
    for i, (name, price) in enumerate(EXTRA_SERVICES):
        c1, c2 = st.columns([3, 1])
        on = c1.checkbox(name, key=f"c_s{i}_{k}")
        val = c2.number_input(name, min_value=0.0, step=10.0, value=price,
                              key=f"c_sp{i}_{k}", label_visibility="collapsed")
        if on:
            extras_total += val
            chosen.append((name, val))

    # VAT
    vat_fee = 0.0
    vat_on = st.checkbox("Сопровождение возврата VAT (процент от суммы европейского НДС)",
                         key=f"c_vat_{k}")
    if vat_on:
        v1, v2 = st.columns(2)
        vat_pct_service = v1.number_input("Комиссия за возврат VAT, %", min_value=0.0, value=15.0,
                                          step=0.5, key=f"c_vatp_{k}")
        vat_mode = v2.radio("НДС задать как", ["Ставка, %", "Сумма, €"], horizontal=True,
                            key=f"c_vatm_{k}")
        if vat_mode == "Ставка, %":
            vat_rate = st.number_input("Ставка НДС, % (от Hammer + сбор аукциона)", min_value=0.0,
                                       value=21.0, step=0.5, key=f"c_vatr_{k}")
            vat_amount = (hammer + premium) * vat_rate / 100.0
        else:
            vat_amount = st.number_input("Сумма европейского НДС, €", min_value=0.0, step=10.0,
                                         key=f"c_vata_{k}")
        vat_fee = vat_amount * vat_pct_service / 100.0
        st.caption(f"НДС: {eur(vat_amount)} → услуга: {eur(vat_fee)}")
        chosen.append((f"Сопровождение возврата VAT ({vat_pct_service:g}% от {eur(vat_amount)})", vat_fee))

    # --- Логистика
    l1, l2 = st.columns(2)
    log_base = l1.number_input("Базовая стоимость доставки, €", min_value=0.0, step=50.0,
                               key=f"c_lb_{k}")
    margin = l2.number_input("Корпоративная маржа, %", min_value=0.0, value=13.0, step=0.5,
                             key=f"c_lm_{k}")
    logistics = log_base * (1 + margin / 100.0)

    # --- Итог
    total = hammer + premium + commission + extras_total + vat_fee + logistics
    rows = [
        ("Цена лота (Hammer Price)", hammer),
        ("Сбор аукциона (Buyer's Premium)", premium),
        ("Комиссия сервиса за выкуп", commission),
        ("Дополнительные услуги", extras_total + vat_fee),
        (f"Логистика (база × {1 + margin / 100:.2f})", logistics),
    ]
    st.divider()
    md = "| Статья | Сумма |\n|---|---:|\n" + "\n".join(f"| {n} | {eur(v)} |" for n, v in rows)
    st.markdown(md)
    st.metric("ИТОГО К ОПЛАТЕ ПОД КЛЮЧ", eur(total))

    # Текстовая смета для клиента
    lines = ["EVO SYSTEMS s.r.o. — смета «под ключ»", f"Дата: {date.today().isoformat()}"]
    if sel:
        lines.append(f"Сделка: {sel['id']} / {sel['client']} / {sel['lot']}")
    lines.append("")
    lines += [f"{n}: {eur(v)}" for n, v in rows]
    if chosen:
        lines.append("")
        lines.append("Состав доп. услуг:")
        lines += [f"  - {n}: {eur(v)}" for n, v in chosen]
    lines += ["", f"ИТОГО К ОПЛАТЕ ПОД КЛЮЧ: {eur(total)}"]
    estimate_txt = "\n".join(lines)

    b1, b2 = st.columns(2)
    b1.download_button("⬇️ Скачать смету (.txt)", estimate_txt.encode("utf-8"),
                       file_name=f"estimate_{k}.txt", mime="text/plain")
    if is_admin and sel:
        if b2.button("💾 Сохранить итог в сделку"):
            sel["estimate_total"] = round(total, 2)
            sel["estimate_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            save_deals(deals)
            st.success(f"Смета {eur(total)} сохранена в {sel['id']}")


# ----------------------------------------------------------------------------
# Документы
# ----------------------------------------------------------------------------
def render_documents(deal: dict, is_admin: bool):
    st.markdown("#### 🗂 Документы")
    st.caption(f"Главная папка: `{deal['folder']}`")
    st.session_state.setdefault("up_counter", 0)
    can_download = is_admin or MANAGER_CAN_DOWNLOAD

    for disk, label in SUBFOLDERS:
        folder = deal_root(deal) / disk
        folder.mkdir(parents=True, exist_ok=True)
        files = sorted([p for p in folder.iterdir() if p.is_file()], key=lambda p: p.name.lower())
        with st.expander(f"{label} ({len(files)})"):
            if not files:
                st.caption("Файлов пока нет.")
            for p in files:
                size_kb = p.stat().st_size / 1024
                mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%d.%m.%Y %H:%M")
                c1, c2, c3 = st.columns([5, 2, 2])
                c1.markdown(f"📄 **{p.name}**  \n<small>{size_kb:,.1f} КБ · {mtime}</small>",
                            unsafe_allow_html=True)
                if can_download:
                    c2.download_button("Скачать", p.read_bytes(), file_name=p.name,
                                       key=f"dl_{deal['id']}_{disk}_{p.name}")
                if is_admin and c3.button("🗑 Удалить", key=f"rm_{deal['id']}_{disk}_{p.name}"):
                    p.unlink(missing_ok=True)
                    st.rerun()

            if is_admin:
                up = st.file_uploader(
                    f"Загрузить в «{label}»", accept_multiple_files=True,
                    key=f"up_{deal['id']}_{disk}_{st.session_state['up_counter']}",
                )
                if up and st.button("⬆️ Сохранить файлы", key=f"upb_{deal['id']}_{disk}"):
                    for f in up:
                        dest = folder / safe_name(f.name)
                        if dest.exists():
                            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                            dest = folder / f"{dest.stem}_{stamp}{dest.suffix}"
                        dest.write_bytes(f.getbuffer())
                    st.session_state["up_counter"] += 1
                    st.rerun()


# ----------------------------------------------------------------------------
# Карточка сделки
# ----------------------------------------------------------------------------
def render_deal_card(deals: list, deal: dict, is_admin: bool):
    if st.button("← К реестру"):
        st.session_state["open_deal"] = None
        st.rerun()

    st.markdown(f"### {html.escape(deal['id'])} &nbsp; {badge(deal)}", unsafe_allow_html=True)
    # «Закреп» с контактами — всегда на самом верху карточки
    st.markdown(contact_html(deal), unsafe_allow_html=True)

    st.markdown(f"**👤 {html.escape(deal['client'])}** — {html.escape(deal['lot'])}")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Hammer Price", eur(deal["hammer"]))
    m2.metric("Buyer's Premium", eur(deal["premium"]))
    m3.metric("Лот + сбор", eur(deal["hammer"] + deal["premium"]))
    m4.metric("Дедлайн", parse_date(deal["deadline"]).strftime("%d.%m.%Y"))
    if is_overdue(deal):
        st.markdown('<span class="overdue">⚠️ Дедлайн просрочен</span>', unsafe_allow_html=True)
    if deal.get("estimate_total"):
        st.caption(f"💶 Сохранённая смета под ключ: {eur(deal['estimate_total'])} "
                   f"({deal.get('estimate_at', '')})")
    if deal.get("notes"):
        st.info(deal["notes"])

    if is_admin:
        with st.expander("🔄 Статус"):
            keys = list(STATUSES.keys())
            new_status = st.selectbox("Статус", keys, index=keys.index(deal["status"]),
                                      format_func=lambda s: STATUSES[s][0],
                                      key=f"st_{deal['id']}")
            new_stage = deal.get("stage", ACTIVE_STAGES[0])
            if new_status == "active":
                idx = ACTIVE_STAGES.index(new_stage) if new_stage in ACTIVE_STAGES else 0
                new_stage = st.selectbox("Этап", ACTIVE_STAGES, index=idx, key=f"stg_{deal['id']}")
            if st.button("Применить статус", key=f"stb_{deal['id']}"):
                deal["status"], deal["stage"] = new_status, new_stage
                save_deals(deals)
                st.rerun()

        with st.expander("✏️ Редактировать сделку"):
            with st.form(f"edit_{deal['id']}"):
                client = st.text_input("Клиент", deal["client"])
                lot = st.text_input("Название лота", deal["lot"])
                c1, c2 = st.columns(2)
                hammer = c1.number_input("Hammer Price, €", min_value=0.0, step=100.0,
                                         value=float(deal["hammer"]))
                premium = c2.number_input("Buyer's Premium, €", min_value=0.0, step=10.0,
                                          value=float(deal["premium"]))
                deadline = st.date_input("Дедлайн", parse_date(deal["deadline"]))
                phone = st.text_input("Телефон", deal.get("phone", ""))
                tg = st.text_input("Telegram", deal.get("telegram", ""))
                mail = st.text_input("Email", deal.get("email", ""))
                req = st.text_area("Реквизиты", deal.get("requisites", ""))
                notes = st.text_area("Заметки", deal.get("notes", ""))
                if st.form_submit_button("Сохранить изменения"):
                    if not client.strip() or not lot.strip():
                        st.error("Укажите клиента и лот.")
                    else:
                        deal.update(client=client.strip(), lot=lot.strip(), hammer=hammer,
                                    premium=premium, deadline=deadline.isoformat(),
                                    phone=phone.strip(), telegram=tg.strip(), email=mail.strip(),
                                    requisites=req.strip(), notes=notes.strip())
                        save_deals(deals)
                        st.rerun()

        with st.expander("🗑 Удалить сделку"):
            st.warning("Будет удалена сделка и ВСЕ загруженные файлы. Действие необратимо.")
            ok = st.checkbox("Подтверждаю удаление", key=f"delc_{deal['id']}")
            if st.button("Удалить навсегда", disabled=not ok, key=f"delb_{deal['id']}"):
                shutil.rmtree(deal_root(deal), ignore_errors=True)
                deals[:] = [d for d in deals if d["id"] != deal["id"]]
                save_deals(deals)
                st.session_state["open_deal"] = None
                st.rerun()

    render_documents(deal, is_admin)


# ----------------------------------------------------------------------------
# Реестр
# ----------------------------------------------------------------------------
def render_registry(deals: list, is_admin: bool):
    open_id = st.session_state.get("open_deal")
    if open_id:
        deal = find_deal(deals, open_id)
        if deal:
            render_deal_card(deals, deal, is_admin)
            return
        st.session_state["open_deal"] = None

    st.subheader("📋 Реестр сделок")
    if not deals:
        st.info("Сделок пока нет." + (" Создайте первую во вкладке «➕ Новая сделка»." if is_admin else ""))
        return

    f1, f2 = st.columns([2, 3])
    q = f1.text_input("🔎 Поиск (номер, клиент, лот)", "").strip().lower()
    flt = f2.multiselect("Статус", list(STATUSES.keys()), default=list(STATUSES.keys()),
                         format_func=lambda s: STATUSES[s][0])

    shown = [d for d in deals if d["status"] in flt and
             (not q or q in f"{d['id']} {d['client']} {d['lot']}".lower())]
    shown.sort(key=lambda d: d["deadline"])
    st.caption(f"Показано: {len(shown)} из {len(deals)}")

    for d in shown:
        with st.container(border=True):
            st.markdown(f"**{html.escape(d['id'])}** &nbsp; {badge(d)}", unsafe_allow_html=True)
            st.markdown(f"👤 {html.escape(d['client'])} — {html.escape(d['lot'])}")
            dl = parse_date(d["deadline"]).strftime("%d.%m.%Y")
            warn = ' <span class="overdue">⚠️ просрочен</span>' if is_overdue(d) else ""
            st.markdown(
                f"💶 Hammer {eur(d['hammer'])} + сбор {eur(d['premium'])} · ⏰ {dl}{warn}",
                unsafe_allow_html=True,
            )
            bits = [x for x in (d.get("phone"), d.get("telegram"), d.get("email")) if x]
            if bits:
                st.caption("📌 " + " · ".join(bits))
            if st.button("Открыть карточку →", key=f"open_{d['id']}"):
                st.session_state["open_deal"] = d["id"]
                st.rerun()


# ----------------------------------------------------------------------------
# Новая сделка
# ----------------------------------------------------------------------------
def render_new_deal(deals: list):
    st.subheader("➕ Новая сделка")
    with st.form("new_deal", clear_on_submit=True):
        client = st.text_input("Имя клиента *")
        lot = st.text_input("Название лота *")
        c1, c2 = st.columns(2)
        hammer = c1.number_input("Ставка (Hammer Price), €", min_value=0.0, step=100.0)
        premium = c2.number_input("Сбор аукциона (Buyer's Premium), €", min_value=0.0, step=10.0)
        deadline = st.date_input("Дедлайн", date.today())
        st.markdown("**Контакты получателя**")
        phone = st.text_input("Телефон")
        tg = st.text_input("Telegram (@username)")
        mail = st.text_input("Email")
        req = st.text_area("Реквизиты")
        notes = st.text_area("Заметки")
        c3, c4 = st.columns(2)
        status = c3.selectbox("Статус", list(STATUSES.keys()), format_func=lambda s: STATUSES[s][0])
        stage = c4.selectbox("Этап (для «Активна»)", ACTIVE_STAGES)

        if st.form_submit_button("Создать сделку"):
            if not client.strip() or not lot.strip():
                st.error("Укажите имя клиента и название лота.")
            else:
                deal_id = next_deal_id(deals)
                deal = {
                    "id": deal_id,
                    "client": client.strip(),
                    "lot": lot.strip(),
                    "hammer": hammer,
                    "premium": premium,
                    "deadline": deadline.isoformat(),
                    "status": status,
                    "stage": stage,
                    "phone": phone.strip(),
                    "telegram": tg.strip(),
                    "email": mail.strip(),
                    "requisites": req.strip(),
                    "notes": notes.strip(),
                    "folder": f"{safe_name(client.strip())} - {deal_id}",
                    "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
                }
                ensure_folders(deal)
                deals.append(deal)
                save_deals(deals)
                st.success(f"Сделка {deal_id} создана. Папки документов подготовлены.")


# ----------------------------------------------------------------------------
# Аналитика
# ----------------------------------------------------------------------------
def render_analytics(deals: list):
    st.subheader("📊 Аналитика")
    if not deals:
        st.info("Данных пока нет.")
        return

    active = [d for d in deals if d["status"] == "active"]
    in_work = [d for d in deals if d["status"] in ("active", "transit")]
    volume = sum(d["hammer"] for d in in_work)
    volume_all = sum(d["hammer"] + d["premium"] for d in in_work)

    c1, c2 = st.columns(2)
    c1.metric("Всего сделок", len(deals))
    c2.metric("Активных", len(active))
    c3, c4 = st.columns(2)
    c3.metric("Объём лотов в работе (Hammer)", eur(volume))
    c4.metric("Объём в работе (Hammer + сбор)", eur(volume_all))
    st.caption("«В работе» = статусы «Активна» и «В пути в СНГ».")

    counts = pd.Series([STATUSES[d["status"]][0] for d in deals]).value_counts()
    st.markdown("**Сделки по статусам**")
    st.bar_chart(counts)

    df = pd.DataFrame([{
        "Сделка": d["id"],
        "Клиент": d["client"],
        "Лот": d["lot"],
        "Статус": status_label(d),
        "Hammer, €": d["hammer"],
        "Сбор, €": d["premium"],
        "Лот + сбор, €": d["hammer"] + d["premium"],
        "Дедлайн": parse_date(d["deadline"]).strftime("%d.%m.%Y"),
        "Смета под ключ, €": d.get("estimate_total"),
    } for d in deals])
    st.markdown("**Сводная таблица**")
    st.dataframe(df, hide_index=True)
    st.download_button("⬇️ Экспорт в CSV", df.to_csv(index=False).encode("utf-8-sig"),
                       file_name="deals_summary.csv", mime="text/csv")


# ----------------------------------------------------------------------------
# Главная
# ----------------------------------------------------------------------------
def get_admin_pin() -> str:
    pin = os.environ.get("ADMIN_PIN", "")
    if not pin:
        try:
            pin = str(st.secrets.get("ADMIN_PIN", ""))
        except Exception:
            pin = ""
    return pin


def main():
    st.sidebar.title("🔨 EU-CIS Auction Hub")
    st.sidebar.caption("EVO SYSTEMS s.r.o.")
    role = st.sidebar.radio("Роль", [ROLE_ADMIN, ROLE_MANAGER])

    is_admin = role == ROLE_ADMIN
    pin = get_admin_pin()
    if is_admin and pin:
        entered = st.sidebar.text_input("PIN администратора", type="password")
        if entered != pin:
            st.sidebar.warning("Введите PIN — пока доступ как у менеджера.")
            is_admin = False

    if is_admin:
        st.sidebar.success("Полный доступ")
    else:
        st.sidebar.info("Только просмотр: реестр, калькулятор, статусы, аналитика")

    deals = load_deals()

    tab_names = ["📋 Реестр", "🧮 Калькулятор", "📊 Аналитика"]
    if is_admin:
        tab_names.append("➕ Новая сделка")
    tabs = st.tabs(tab_names)

    with tabs[0]:
        render_registry(deals, is_admin)
    with tabs[1]:
        render_calculator(deals, is_admin)
    with tabs[2]:
        render_analytics(deals)
    if is_admin:
        with tabs[3]:
            render_new_deal(deals)


main()
