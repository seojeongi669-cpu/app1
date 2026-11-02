"""커플 다이어리 | Streamlit Community Cloud 배포

[최초 설정]
1. GitHub에 이 app.py와 requirements.txt를 올립니다.
2. 일기 저장용 비공개 GitHub 저장소를 하나 만들고 README를 추가합니다.
   소스 저장소와 같아도 되지만, 일기가 담기는 저장소는 반드시 비공개여야 합니다.
3. 저장용 저장소에만 Contents: Read and write 권한을 가진 fine-grained PAT를 만듭니다.
4. Streamlit Community Cloud에서 app.py를 배포하고 Settings > Secrets에 입력합니다.

GITHUB_TOKEN = "발급한 토큰"
GITHUB_REPO = "내계정/비공개저장소"
GITHUB_BRANCH = "main"
COUPLE_CODE = "둘만아는-충분히-긴-공통암호"
PERSON1_NAME = "나"
PERSON2_NAME = "너"
PERSON1_PIN = "서로다른-6자리이상-PIN"
PERSON2_PIN = "서로다른-6자리이상-PIN"
APP_URL = "https://배포주소.streamlit.app"

5. APP_URL 설정 후 앱의 QR 버튼으로 QR을 내려받아 공유합니다.

주의: 토큰/암호는 코드에 넣지 마세요. PIN은 서로 다르게 설정하세요.
GitHub 저장소에 일기 본문은 평문으로 저장됩니다(종단간 암호화 아님).
저장소 관리자와 배포 관리자는 본문에 접근할 수 있습니다.
한 사람당 날짜별 한 편이며 다시 저장하면 해당 날짜 일기를 수정합니다.
수정하면 상대의 읽음 표시가 초기화됩니다. 날짜는 한국 시간 기준입니다.
"""

import base64
import hashlib
import hmac
import io
import json
import secrets
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urlparse

import qrcode
import requests
import streamlit as st

st.set_page_config(page_title="둘의 하루", page_icon="💌", layout="centered", initial_sidebar_state="collapsed")
st.markdown("""
<style>
[data-testid="stAppViewContainer"] {background:#fff9f7;}
header[data-testid="stHeader"] {background:transparent;height:0;}
.block-container {max-width:480px;padding:0.65rem 0.85rem 0.5rem;}
[data-testid="stVerticalBlock"] {gap:0.35rem;}
h1 {font-size:1.45rem !important;padding:0 !important;}
p {margin-bottom:0.2rem;}
[data-testid="stCaptionContainer"] p {font-size:0.75rem;}
.stButton button,.stFormSubmitButton button {border-radius:14px;min-height:2.15rem;}
[data-testid="stTextArea"] textarea {font-size:16px;}
[data-testid="stTextInput"] input {font-size:16px;}
[data-testid="stForm"] {padding:0.55rem;border-radius:18px;background:white;}
.diary {height:112px;overflow-y:auto;white-space:pre-wrap;overflow-wrap:anywhere;
 background:white;border:1px solid #f2deda;border-radius:14px;padding:10px;
 font-size:0.92rem;line-height:1.5;}
.small {font-size:0.76rem;color:#876c70;}
@media(max-height:700px) {
 .block-container {padding-top:0.3rem;}
 [data-testid="stVerticalBlock"] {gap:0.2rem;}
 .diary {height:82px;}
}
</style>
""", unsafe_allow_html=True)

KST = timezone(timedelta(hours=9))
MOODS = ["😊 행복", "🥰 설렘", "😌 평온", "😢 속상", "😴 피곤", "😤 답답"]


class StoreError(Exception):
    pass


def config():
    required = ["GITHUB_TOKEN", "GITHUB_REPO", "COUPLE_CODE", "PERSON1_PIN", "PERSON2_PIN"]
    try:
        cfg = {key: str(st.secrets[key]) for key in required}
        for key, default in [("GITHUB_BRANCH", "main"), ("PERSON1_NAME", "나"),
                             ("PERSON2_NAME", "너"), ("APP_URL", "")]:
            cfg[key] = str(st.secrets.get(key, default))
    except Exception:
        st.info("처음 오셨네요 💌 배포 관리자가 Secrets를 설정하면 시작할 수 있어요.")
        st.caption("app.py 맨 위에 설정 방법이 있습니다. 암호와 PIN은 따옴표로 감싸 입력하세요.")
        st.stop()
    if (any(not cfg[k] for k in required) or len(cfg["GITHUB_REPO"].split("/")) != 2
            or min(len(cfg["PERSON1_PIN"]), len(cfg["PERSON2_PIN"])) < 6
            or cfg["PERSON1_PIN"] == cfg["PERSON2_PIN"]):
        st.error("Secrets를 확인하세요: 저장소는 소유자/저장소 형식, PIN은 서로 다른 6글자 이상이어야 해요.")
        st.stop()
    return cfg


CFG = config()
NAMES = {"p1": CFG["PERSON1_NAME"], "p2": CFG["PERSON2_NAME"]}
# 암호 변경 시에도 기존 일기 파일을 유지합니다. 저장소 하나를 커플 하나가 사용합니다.
PATH = "couple-diary/diary.json"
API = "https://api.github.com/repos/" + "/".join(quote(x, safe="") for x in CFG["GITHUB_REPO"].split("/"))
HEADERS = {"Authorization": "Bearer " + CFG["GITHUB_TOKEN"],
           "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def call(method, url, **kwargs):
    try:
        return requests.request(method, url, headers=HEADERS, timeout=15, **kwargs)
    except requests.RequestException:
        raise StoreError("저장소 연결이 지연되고 있어요. 잠시 후 다시 시도해 주세요.") from None


@st.cache_data(ttl=120, show_spinner=False)
def verify_private(repo, branch, token):
    # 공개 저장소에 민감한 일기를 잘못 쓰는 일을 막습니다.
    response = call("GET", API)
    if response.status_code != 200:
        raise StoreError("저장소에 접근할 수 없어요. 저장소 이름과 토큰 권한을 확인하세요.")
    if not response.json().get("private"):
        raise StoreError("일기를 보호하려면 저장소를 비공개로 설정해야 해요.")
    response = call("GET", API + "/branches/" + quote(branch, safe=""))
    if response.status_code != 200:
        raise StoreError("저장소에 README를 만들고 GITHUB_BRANCH 설정을 확인하세요.")
    return True


def read_store():
    verify_private(CFG["GITHUB_REPO"], CFG["GITHUB_BRANCH"], CFG["GITHUB_TOKEN"])
    response = call("GET", API + "/contents/" + PATH,
                    params={"ref": CFG["GITHUB_BRANCH"]})
    if response.status_code == 404:
        return {"version": 1, "entries": {}}, None
    if response.status_code != 200:
        raise StoreError("일기를 불러오지 못했어요. 토큰 만료·권한·요청 한도를 확인해 주세요.")
    try:
        payload = response.json()
        data = json.loads(base64.b64decode(payload["content"]).decode("utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("entries"), dict):
            raise ValueError()
        return data, payload["sha"]
    except (ValueError, KeyError, TypeError):
        raise StoreError("저장 파일 형식이 올바르지 않아요. 원본을 확인해 주세요. 덮어쓰지 않았어요.") from None


def mutate(operation):
    # GitHub SHA 비교로 동시 쓰기 시 덮어쓰기 방지. 충돌하면 최신본에 재적용합니다.
    for attempt in range(4):
        data, sha = read_store()
        changed = operation(data)
        if changed is False:
            return False
        raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        if len(raw) > 800_000:
            raise StoreError("일기 저장 용량이 커졌어요. 관리자가 백업·정리한 뒤 다시 시도해 주세요.")
        body = {"message": "Update private diary", "branch": CFG["GITHUB_BRANCH"],
                "content": base64.b64encode(raw).decode("ascii")}
        if sha:
            body["sha"] = sha
        response = call("PUT", API + "/contents/" + PATH, json=body)
        if response.status_code in (200, 201):
            return True
        if response.status_code in (409, 422):
            time.sleep(0.25 * (attempt + 1))
            continue
        raise StoreError("저장하지 못했어요. 토큰의 Contents 쓰기 권한이나 만료를 확인해 주세요.")
    raise StoreError("두 사람이 동시에 저장했어요. 글은 입력창에 남아 있으니 다시 저장해 주세요.")


def fingerprint():
    raw = "\0".join(CFG[k] for k in ["COUPLE_CODE", "PERSON1_PIN", "PERSON2_PIN"])
    return hashlib.sha256(raw.encode()).hexdigest()


st.markdown("# 💌 둘의 하루")
if st.session_state.get("auth_version") != fingerprint():
    st.session_state.pop("person", None)

if "person" not in st.session_state:
    st.caption("너의 하루를 읽고, 내 마음을 남기는 작은 공간")
    with st.form("login"):
        person = st.selectbox("나는", ["p1", "p2"], format_func=lambda p: NAMES[p])
        code = st.text_input("둘만의 공통 암호", type="password")
        pin = st.text_input("나의 PIN", type="password")
        submit = st.form_submit_button("우리 공간 들어가기", use_container_width=True)
    if submit:
        now = time.time()
        if now < st.session_state.get("blocked_until", 0):
            st.error("잠시 기다렸다가 다시 시도해 주세요.")
        else:
            expected = CFG["PERSON1_PIN" if person == "p1" else "PERSON2_PIN"]
            ok_code = hmac.compare_digest(code.encode(), CFG["COUPLE_CODE"].encode())
            ok_pin = hmac.compare_digest(pin.encode(), expected.encode())
            if ok_code and ok_pin:
                st.session_state.update(person=person, auth_version=fingerprint(), failures=0)
                st.rerun()
            else:
                failures = st.session_state.get("failures", 0) + 1
                st.session_state["failures"] = failures
                st.session_state["blocked_until"] = now + min(60, 2 ** min(failures, 6))
                st.error("공통 암호 또는 PIN을 확인해 주세요.")
    st.caption("🔒 개인용 간편 인증입니다. 로그인 제한은 접속 세션 기준이에요.")
    st.stop()

me = st.session_state.person
partner = "p2" if me == "p1" else "p1"
now = datetime.now(KST)
with st.container(horizontal=True, vertical_alignment="center"):
    st.caption(f"{NAMES[me]} ↔ {NAMES[partner]}")
    with st.popover("⚙️", help="날짜·QR·로그아웃"):
        selected = st.date_input("일기 날짜", value=now.date(), max_value=now.date(), key="day")
        st.caption("날짜를 바꾸면 지난 일기도 볼 수 있어요.")
        url = CFG["APP_URL"].strip()
        parsed = urlparse(url)
        if parsed.scheme == "https" and parsed.netloc and not parsed.query and not parsed.fragment:
            image = qrcode.make(url)
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            st.image(buf.getvalue(), width=180)
            st.download_button("QR 저장", buf.getvalue(), "couple-qr.png", "image/png")
            st.caption("QR에는 앱 주소만 들어 있어요. 암호와 PIN은 별도로 전달하세요.")
        else:
            st.caption("Secrets의 APP_URL에 쿼리 없는 https 배포 주소를 설정하면 QR이 표시돼요.")
        st.caption("본문은 비공개 저장소에 평문 저장됩니다. 관리자는 열람할 수 있어요.")
        if st.button("로그아웃", use_container_width=True):
            st.session_state.clear()
            st.rerun()

day = selected.isoformat()
try:
    initial, _ = read_store()
except StoreError as exc:
    st.error(str(exc))
    st.stop()

own_key = day + ":" + me
other_key = day + ":" + partner
own = initial["entries"].get(own_key, {})
st.caption(f"{selected.strftime('%m월 %d일')} · 내 하루를 남겨요 (최대 500자)")
with st.form("write_" + own_key, clear_on_submit=False):
    mood = st.selectbox("오늘 기분", MOODS,
                        index=MOODS.index(own.get("mood")) if own.get("mood") in MOODS else 0,
                        label_visibility="collapsed")
    text = st.text_area("오늘의 일기", value=own.get("text", ""), max_chars=500,
                        height=95, placeholder="오늘 너에게 들려주고 싶은 이야기…", label_visibility="collapsed")
    submitted = st.form_submit_button("💗 일기 저장 / 수정", use_container_width=True, type="primary")
if submitted:
    if not text.strip():
        st.warning("일기를 한 줄이라도 적어 주세요.")
    else:
        def save(data):
            old = data["entries"].get(own_key, {})
            if old.get("text") == text.strip() and old.get("mood") == mood:
                return False
            data["entries"][own_key] = {
                "author": me, "date": day, "mood": mood, "text": text.strip(),
                "updated_at": datetime.now(KST).isoformat(),
                "revision": secrets.token_hex(12), "seen_by": {}}
            return True
        try:
            changed = mutate(save)
            st.toast("저장했어요 💌" if changed else "이미 같은 내용으로 저장되어 있어요.")
        except StoreError as exc:
            st.error(str(exc))


@st.fragment(run_every="20s")
def shared_diary():
    import html
    try:
        data, _ = read_store()
    except StoreError as exc:
        st.caption(str(exc))
        return
    mine = data["entries"].get(own_key, {})
    theirs = data["entries"].get(other_key, {})
    if mine:
        seen = mine.get("seen_by", {}).get(partner)
        st.caption(f"내 일기 · {'✓ 상대가 확인했어요' if seen else '상대의 확인을 기다리는 중'}")
    else:
        st.caption("내 일기 · 아직 작성 전이에요")
    st.markdown(f"**{html.escape(NAMES[partner])}의 하루** · {html.escape(theirs.get('mood', '💭 기다리는 중'))}")
    body = html.escape(theirs.get("text", "아직 남긴 일기가 없어요. 조금만 기다려 볼까요?"))
    st.markdown(f'<div class="diary">{body}</div>', unsafe_allow_html=True)
    is_seen = bool(theirs.get("seen_by", {}).get(me))
    read_clicked = st.checkbox("확인했어요 💕", value=is_seen,
                               key="read_" + other_key + "_" + theirs.get("revision", "none"),
                               disabled=not bool(theirs))
    if theirs and read_clicked != is_seen:
        revision = theirs.get("revision")
        def mark(data):
            entry = data["entries"].get(other_key)
            if not entry or entry.get("revision") != revision:
                raise StoreError("상대가 일기를 수정했어요. 새 내용을 읽고 다시 체크해 주세요.")
            seen_by = entry.setdefault("seen_by", {})
            if read_clicked:
                seen_by[me] = datetime.now(KST).isoformat()
            else:
                seen_by.pop(me, None)
            return True
        try:
            mutate(mark)
            st.rerun(scope="fragment")
        except StoreError as exc:
            st.error(str(exc))
    st.caption("20초마다 읽음 상태 갱신 · 긴 일기만 본문 안에서 스크롤")

shared_diary()
