import os, json, time, logging, datetime, decimal, requests

from . import config, db, email_notify, license

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

STATE_FILE = "/data/state.json"
EB_API     = "https://api.enablebanking.com"

def _own_names():
    val = config.ACCOUNT_HOLDER_NAME or ""
    return {n.strip().lower() for n in val.split(",") if n.strip()}

def _make_headers():
    import jwt, uuid
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    key_data = open("/data/private.pem", "rb").read()
    key = load_pem_private_key(key_data, password=None)
    now = int(time.time())
    payload = {
        "iss": "enablebanking.com", "aud": "api.enablebanking.com",
        "iat": now, "exp": now + 3600,
        "jti": str(uuid.uuid4()), "sub": config.EB_APPLICATION_ID
    }
    token = jwt.encode(payload, key, algorithm="RS256", headers={"kid": config.EB_APPLICATION_ID})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def _load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def _save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def _get_session(state):
    sid = state.get("eb_session_id") or db.get_setting("eb_session_id")
    uid = state.get("eb_account_uid") or db.get_setting("eb_account_uid")
    exp = state.get("eb_session_expiry") or db.get_setting("eb_session_expiry")
    if not sid or not uid:
        raise RuntimeError("No session found. Complete bank authorisation in the web UI.")
    expiry = datetime.datetime.fromisoformat(exp)
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=datetime.timezone.utc)
    days_left = (expiry - datetime.datetime.now(datetime.timezone.utc)).days
    if days_left < 7:
        log.warning("Session expires in %d days.", days_left)
        email_notify.send_session_expiry_warning(days_left)
    return sid, uid

def _fetch_transactions(account_uid, date_from):
    headers = _make_headers()
    params  = {"date_from": date_from.isoformat(), "date_to": datetime.date.today().isoformat()}
    txns    = []
    url     = f"{EB_API}/accounts/{account_uid}/transactions"
    while url:
        r = requests.get(url, headers=headers, params=params)
        if not r.ok:
            log.error("Enable Banking error %s: %s", r.status_code, r.text)
            r.raise_for_status()
        data = r.json()
        txns.extend(data.get("transactions", []))
        ck  = data.get("continuation_key")
        url = f"{EB_API}/accounts/{account_uid}/transactions" if ck else None
        params = {"continuation_key": ck} if ck else {}
    log.info("Fetched %d transactions from Enable Banking", len(txns))
    return txns

def _parse_date(t):
    raw = t.get("transaction_date") or t.get("booking_date") or t.get("value_date")
    if not raw: raise ValueError("No date")
    return datetime.date.fromisoformat(raw[:10])

def _parse_amount(t):
    amt   = decimal.Decimal(str((t.get("transaction_amount") or {}).get("amount", "0")))
    indic = t.get("credit_debit_indicator") or t.get("credit_debit_indic", "")
    return -abs(amt) if indic.upper() == "DBIT" else abs(amt)

def _parse_payee(t):
    own   = _own_names()
    indic = (t.get("credit_debit_indicator") or t.get("credit_debit_indic", "")).upper()
    if indic == "DBIT":
        name = (t.get("creditor") or {}).get("name") or t.get("creditor_name")
        if not name:
            ri = t.get("remittance_information")
            name = ri[0] if isinstance(ri, list) else ri
    else:
        name = (t.get("debtor") or {}).get("name") or t.get("debtor_name")
        if not name or (own and name.lower() in own):
            ri = t.get("remittance_information")
            name = ri[0] if isinstance(ri, list) else ri
    return name or "Unknown"

def _parse_notes(t):
    ref = t.get("remittance_information_unstructured")
    if ref: return ref
    ri = t.get("remittance_information")
    if ri and isinstance(ri, list): return " ".join(ri)
    return ""

def _get_entry_ref(t):
    return t.get("entry_reference") or t.get("transaction_id") or ""

def run():
    log.info("Starting sync...")

    # License check
    result = license.validate()
    if not result["valid"]:
        msg = f"License invalid: {result['error']}"
        log.error(msg)
        email_notify.send_failure(msg)
        db.log_sync("failure", message=msg)
        return False, 0, msg

    state = _load_state()
    try:
        _, account_uid = _get_session(state)
    except RuntimeError as e:
        msg = str(e)
        log.error(msg)
        db.log_sync("failure", message=msg)
        return False, 0, msg

    last = state.get("last_sync_date") or config.START_SYNC_DATE or None
    if last:
        date_from = datetime.date.fromisoformat(last)
    else:
        date_from = datetime.date.today() - datetime.timedelta(days=30)
        log.info("First run: fetching last 30 days")

    pending_map = state.get("pending_map", {})
    if pending_map:
        earliest = min(datetime.date.fromisoformat(k.split("|")[0]) for k in pending_map)
        if earliest < date_from:
            date_from = earliest

    try:
        raw = _fetch_transactions(account_uid, date_from)
    except requests.HTTPError as e:
        msg = "Enable Banking returned an error. Your session may have expired."
        log.error(msg)
        db.log_sync("failure", message=msg)
        return False, 0, msg

    if not raw:
        log.info("No new transactions")
        state["last_sync_date"] = datetime.date.today().isoformat()
        _save_state(state)
        db.log_sync("success", tx_count=0)
        return True, 0, "OK"

    imported_refs = set(state.get("imported_refs", []))
    added = updated = skipped = 0

    try:
        from actual import Actual
        from actual.queries import get_or_create_account, reconcile_transaction, get_transactions, create_transaction

        with Actual(base_url=config.ACTUAL_URL, password=config.ACTUAL_PASSWORD,
                    file=config.ACTUAL_SYNC_ID, data_dir="/data/actual-cache") as actual:
            account        = get_or_create_account(actual.session, config.ACTUAL_ACCOUNT)
            existing       = list(get_transactions(actual.session, account=account))
            already_matched = existing[:]
            new_txn        = []

            for txn in raw:
                try:
                    status = txn.get("status", "BOOK")
                    date   = _parse_date(txn)
                    amount = _parse_amount(txn)
                    payee  = _parse_payee(txn)
                    notes  = _parse_notes(txn)
                    ref    = _get_entry_ref(txn)
                    key    = f"{date}|{amount}"

                    if status == "PDNG":
                        if key not in pending_map:
                            try:
                                t = reconcile_transaction(
                                    actual.session, date, account, payee, notes,
                                    None, amount, cleared=False,
                                    already_matched=already_matched, imported_payee=payee
                                )
                            except Exception:
                                t = create_transaction(
                                    actual.session, date, account, payee, notes,
                                    amount, cleared=False, imported_payee=payee
                                )
                            already_matched.append(t)
                            if t.changed():
                                pending_map[key] = str(t.id)
                                added += 1
                                new_txn.append(t)
                            else:
                                skipped += 1
                        else:
                            skipped += 1
                    else:
                        if ref and ref in imported_refs:
                            skipped += 1
                            continue
                        if key in pending_map:
                            txn_id       = pending_map[key]
                            existing_txn = next((t for t in existing if str(t.id) == txn_id), None)
                            if existing_txn:
                                existing_txn.cleared = True
                                del pending_map[key]
                                if ref: imported_refs.add(ref)
                                updated += 1
                            else:
                                del pending_map[key]
                                if ref: imported_refs.add(ref)
                                skipped += 1
                        else:
                            t = reconcile_transaction(
                                actual.session, date, account, payee, notes,
                                None, amount, cleared=True,
                                already_matched=already_matched, imported_payee=payee
                            )
                            already_matched.append(t)
                            if t.changed():
                                if ref: imported_refs.add(ref)
                                new_txn.append(t)
                                added += 1
                            else:
                                skipped += 1
                except Exception as e:
                    log.warning("Skipping transaction: %s | %s", e, txn)

            try:
                actual.run_rules(new_txn)
            except Exception as e:
                log.error("Error applying rules: %s", e)

            actual.commit()
            log.info("Done: %d added, %d confirmed, %d skipped", added, updated, skipped)

    except Exception as e:
        msg = "Could not connect to Actual Budget. Check your URL and password."
        log.error(msg)
        db.log_sync("failure", message=msg)
        email_notify.send_failure(msg)
        return False, 0, msg

    state["last_sync_date"]  = datetime.date.today().isoformat()
    state["pending_map"]     = pending_map
    state["imported_refs"]   = list(imported_refs)
    _save_state(state)
    db.log_sync("success", tx_count=added)
    email_notify.send_success(added)
    return True, added, "OK"
