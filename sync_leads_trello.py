"""Envia para o Trello os leads do Explee que responderam (sem duplicar).

- Leads quentes -> lista "🔥 Novos leads quentes"
- Demais respostas (não quentes) -> lista "Responderam", para contato futuro
- Lê vários projetos do Explee e marca no cartão de qual projeto veio.
Roda a cada ~10 minutos pelo GitHub Actions. Sem estado: o projeto + person_id,
gravados na descrição do cartão, evitam duplicatas.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

EXPLEE_KEY = os.environ["EXPLEE_API_KEY"]
# formato: "id:nome,id:nome"
PROJECTS = [
    item.split(":", 1)
    for item in os.environ.get(
        "EXPLEE_PROJECTS", "54269:williammalta.com.br,68078:lobcapital.com.br"
    ).split(",")
]
LEGACY_PROJECT = "68078"  # cartões antigos, criados antes de existirem 2 projetos
TRELLO_KEY = os.environ["TRELLO_KEY"]
TRELLO_TOKEN = os.environ["TRELLO_TOKEN"]
BOARD_ID = os.environ["TRELLO_BOARD_ID"]
HOT_LIST_ID = os.environ["TRELLO_LIST_ID"]
REPLIED_LIST_ID = os.environ["TRELLO_REPLIED_LIST_ID"]
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "0"))  # 0 = todos os leads quentes
MAX_PAGES = int(os.environ.get("MAX_PAGES", "10"))
MAX_NEW_REPLIED = int(os.environ.get("MAX_NEW_REPLIED", "80"))

EXPLEE_BASE = "https://api.explee.com/public/api/v1"
TRELLO_BASE = "https://api.trello.com/1"


def http(url, method="GET", headers=None, data=None):
    for attempt in range(3):
        request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                body = response.read().decode("utf-8")
            return json.loads(body) if body else None
        except urllib.error.HTTPError as err:
            if err.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            detail = err.read().decode("utf-8", "replace")[:300]
            sys.exit(f"Erro {err.code} em {method} {url.split('?')[0]}: {detail}")
        except urllib.error.URLError as err:
            if attempt < 2:
                time.sleep(5)
                continue
            sys.exit(f"Falha de rede em {url.split('?')[0]}: {err}")


def explee(path, **params):
    params = {k: v for k, v in params.items() if v is not None}
    return http(f"{EXPLEE_BASE}{path}?{urllib.parse.urlencode(params)}",
                headers={"X-API-Key": EXPLEE_KEY})


def trello(path, method="GET", **params):
    url = f"{TRELLO_BASE}{path}?" + urllib.parse.urlencode({"key": TRELLO_KEY, "token": TRELLO_TOKEN})
    data = headers = None
    if method == "GET":
        url += "&" + urllib.parse.urlencode(params)
    else:  # textos longos vão no corpo, senão a URL fica grande demais (erro 414)
        data = urllib.parse.urlencode(params).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
    result = http(url, method, headers, data)
    if method != "GET":
        time.sleep(0.2)  # respeita o limite de requisições do Trello
    return result


def short(text, limit=2000):
    text = str(text or "-").strip()
    return text if len(text) <= limit else text[:limit] + " [...]"


def as_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                return value
    return []


def paged(path, **params):
    items = []
    for page in range(MAX_PAGES):
        batch = as_list(explee(path, limit=200, offset=page * 200, **params))
        items.extend(batch)
        if len(batch) < 200:
            break
    return items


def pick(data, *keys):
    for source in data if isinstance(data, list) else [data]:
        if isinstance(source, dict):
            for key in keys:
                if source.get(key):
                    return source[key]
    return None


def marker(desc, name):
    for line in (desc or "").splitlines():
        line = line.strip("` ")
        if line.startswith(f"{name}:"):
            return line.split(":", 1)[1].strip()
    return None


def tag(project_name):
    return project_name.split(".")[0]


def hot_description(lead, project_id, project_name):
    return "\n".join([
        f"**Projeto:** {project_name}",
        "",
        f"**Por que é quente (resposta do lead):**\n> {short(lead.get('why_hot'))}",
        "",
        f"- Cargo: {lead.get('job_title') or '-'}",
        f"- Empresa: {lead.get('company_name') or '-'} ({lead.get('company_domain') or '-'})",
        f"- E-mail: {lead.get('email') or '-'}",
        f"- Telefone: {lead.get('phone') or '-'}",
        f"- LinkedIn: {lead.get('linkedin_url') or '-'}",
        f"- País: {lead.get('country') or '-'}",
        f"- Ficou quente em: {lead.get('became_hot_at') or '-'}",
        "",
        f"`explee_project_id: {project_id}`",
        f"`explee_person_id: {lead['person_id']}`",
        f"`explee_became_hot_at: {lead.get('became_hot_at') or ''}`",
    ])


def sync_hot(cards, project_id, project_name):
    since = None
    if LOOKBACK_HOURS > 0:
        since = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).isoformat()
    leads = paged("/autogtm/hot-leads", project_id=project_id, source="agent", since=since)
    print(f"[{project_name}] Leads quentes retornados: {len(leads)}")
    created = updated = moved = 0
    for lead in sorted(leads, key=lambda item: item.get("became_hot_at") or ""):
        if not lead.get("person_id"):
            continue
        key = f"{project_id}:{lead['person_id']}"
        title = f"[{tag(project_name)}] {lead.get('name') or lead.get('email') or lead['person_id']}"
        if lead.get("company_name"):
            title += f" — {lead['company_name']}"
        desc = hot_description(lead, project_id, project_name)
        card = cards.get(key)
        if card is None:
            new = trello("/cards", "POST", idList=HOT_LIST_ID, name=title, desc=desc, pos="top")
            cards[key] = {"id": new["id"], "desc": desc, "idList": HOT_LIST_ID}
            created += 1
            continue
        changed = marker(card["desc"], "explee_became_hot_at") != (lead.get("became_hot_at") or "")
        if card["idList"] == REPLIED_LIST_ID:
            trello(f"/cards/{card['id']}", "PUT", idList=HOT_LIST_ID, desc=desc, pos="top")
            trello(f"/cards/{card['id']}/actions/comments", "POST",
                   text=f"Virou lead quente ({lead.get('became_hot_at')}):\n> {short(lead.get('why_hot'))}")
            card.update(idList=HOT_LIST_ID, desc=desc)
            moved += 1
        elif changed:
            trello(f"/cards/{card['id']}", "PUT", desc=desc)
            trello(f"/cards/{card['id']}/actions/comments", "POST",
                   text=f"Respondeu de novo ({lead.get('became_hot_at')}):\n> {short(lead.get('why_hot'))}")
            card["desc"] = desc
            updated += 1
    print(f"[{project_name}] Quentes -> criados: {created} | atualizados: {updated} | movidos de Responderam: {moved}")


def last_message_text(thread):
    messages = []
    if isinstance(thread, dict):
        messages = as_list(thread.get("messages") or thread.get("thread") or thread)
    for message in reversed(messages):
        text = pick(message, "body_text", "text", "body", "content", "snippet")
        if text:
            return str(text).strip()[:1500]
    return None


def sync_replied(cards, project_id, project_name, budget):
    items = paged(f"/autogtm/projects/{project_id}/inbox", tab="replied")
    print(f"[{project_name}] Conversas com resposta retornadas: {len(items)}")
    if items:
        print("Campos de uma conversa (só nomes):", sorted(items[0].keys()))
    created = skipped = 0
    for item in items:
        person_id = str(item.get("person_id") or "")
        campaign_id = item.get("campaign_id")
        key = f"{project_id}:{person_id}"
        if not person_id or not campaign_id or key in cards:
            continue
        intent = str(item.get("latest_intent") or "").lower()
        if item.get("erased") or any(word in intent for word in ("unsub", "bounce")):
            skipped += 1  # apagados, descadastrados ou e-mail inválido: não contatar
            continue
        if created >= budget:
            break
        thread = explee(f"/autogtm/campaigns/{campaign_id}/inbox/{person_id}")
        if created == 0 and isinstance(thread, dict):
            print("Campos da conversa completa (só nomes):", sorted(thread.keys()))
        lead = thread.get("lead") if isinstance(thread, dict) and isinstance(thread.get("lead"), dict) else {}
        name = pick([lead, item], "name", "full_name") or " ".join(
            filter(None, [pick([lead, item], "first_name"), pick([lead, item], "last_name")])
        ) or pick([lead, item], "email") or person_id
        company = pick([lead, item], "company_name", "company")
        title = f"[{tag(project_name)}] {name}" + (f" — {company}" if company else "")
        desc = "\n".join([
            f"**Projeto:** {project_name}",
            "",
            f"**Última mensagem da conversa:**\n> {last_message_text(thread) or '-'}",
            "",
            f"- Assunto: {item.get('latest_subject') or '-'}",
            f"- Intenção detectada: {item.get('latest_intent') or '-'} | Status: {item.get('lead_status') or '-'}",
            f"- Respostas: {item.get('reply_count') or '-'} | Última resposta em: {item.get('latest_reply_at') or '-'}",
            f"- Cargo: {pick([lead, item], 'job_title') or '-'}",
            f"- Empresa: {company or '-'} ({pick([lead, item], 'company_domain') or '-'})",
            f"- E-mail: {pick([lead, item], 'email') or '-'}",
            f"- Telefone: {pick([lead, item], 'phone') or '-'}",
            f"- LinkedIn: {pick([lead, item], 'linkedin_url') or '-'}",
            "",
            f"`explee_project_id: {project_id}`",
            f"`explee_person_id: {person_id}`",
            f"`explee_campaign_id: {campaign_id}`",
        ])
        new = trello("/cards", "POST", idList=REPLIED_LIST_ID, name=title, desc=desc, pos="top")
        cards[key] = {"id": new["id"], "desc": desc, "idList": REPLIED_LIST_ID}
        created += 1
    print(f"[{project_name}] Responderam -> criados: {created} | ignorados (descadastrados/apagados): {skipped}")
    return created


def main():
    # "all" inclui cartões arquivados, para nunca recriar um cartão que você arquivou
    board_cards = trello(f"/boards/{BOARD_ID}/cards/all", fields="id,desc,idList")
    cards = {}
    for card in board_cards:
        person_id = marker(card.get("desc"), "explee_person_id")
        if person_id:
            project = marker(card.get("desc"), "explee_project_id") or LEGACY_PROJECT
            cards[f"{project}:{person_id}"] = {
                "id": card["id"], "desc": card.get("desc") or "", "idList": card["idList"]}
    for project_id, project_name in PROJECTS:
        sync_hot(cards, project_id, project_name)
    budget = MAX_NEW_REPLIED
    for project_id, project_name in PROJECTS:
        budget -= sync_replied(cards, project_id, project_name, budget)
        if budget <= 0:
            print("Limite de cartões por execução atingido; o resto entra na próxima.")
            break


if __name__ == "__main__":
    main()
