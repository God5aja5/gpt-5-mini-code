import os
import json
import uuid
import requests
import datetime
import re
import zipfile
import io
import mimetypes
from flask import Flask, request, Response, jsonify, send_from_directory, stream_with_context, send_file

app = Flask(__name__, static_url_path="", static_folder=".")

# ====== Workik API Setup (GPT-5 mini only) ======
API_URL = "https://wfhbqniijcsamdp2v3i6nts4ke0ebkyj.lambda-url.us-east-1.on.aws/api_ai_playground/ai/playground/ai/trigger"
WORKIK_TOKEN = os.getenv("WORKIK_TOKEN", "undefined")

# Default tokens (will be overridden by tokens.txt if present)
DEFAULT_WK_LD = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0eXBlIjoibG9jYWwiLCJzZXNzaW9uX2lkIjoiMTc1NjM4ODE4MyIsInJlcXVlc3RfY291bnQiOjAsImV4cCI6MTc1Njk5Mjk4M30.JbAEBmTbWtyysFGDftxRJvqy1LvJfIR1W-HVv_Ss-7U"
DEFAULT_WK_CK = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0eXBlIjoiY29va2llIiwic2Vzc2lvbl9pZCI6IjE3NTYzODgxODMiLCJyZXF1ZXN0X2NvdW50IjowLCJleHAiOjE3NTY5OTI5ODN9.Fua9aRmHJLPte8cF807w4jHA6Ff1GPwGAaOWcY9P7Us"

TOKENS_FILE = "tokens.txt"

_current_tokens = {
    "wk_ld": DEFAULT_WK_LD,
    "wk_ck": DEFAULT_WK_CK,
}

headers = {
    "Content-Type": "application/json; charset=utf-8",
    "Authorization": WORKIK_TOKEN,
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118 Safari/537.36",
    "accept": "application/json",
    "x-is-vse": "false",
    "x-vse-version": "0.0.0",
    "sec-ch-ua-platform": '"Linux"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua": '"Not;A=Brand";v="99", "HeadlessChrome";v="139", "Chromium";v="139"',
    "referer": "https://workik.com/"
}

# In-memory last user prompt per session for "continue"
session_last_prompt = {}

def load_tokens_from_file():
    global _current_tokens
    if not os.path.exists(TOKENS_FILE):
        return
    last = None
    with open(TOKENS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                last = rec
            except:
                continue
    if last and "wk_ld" in last and "wk_ck" in last:
        _current_tokens["wk_ld"] = last["wk_ld"]
        _current_tokens["wk_ck"] = last["wk_ck"]

def append_tokens_to_file(wk_ld, wk_ck, coding_language=None):
    os.makedirs(os.path.dirname(TOKENS_FILE) or ".", exist_ok=True)
    entry = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "wk_ld": wk_ld,
        "wk_ck": wk_ck,
    }
    if coding_language is not None:
        entry["codingLanguage"] = coding_language
    with open(TOKENS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

load_tokens_from_file()

def ai_payload(prompt, file_info=None, is_edit_request=False):
    # Always instruct AI to output code inside ```Code Box``` fences
    system_instructions = (
        "You are Gpt 5 mini. Rules:\n"
        "1) Always place any code inside triple backticks with Code Box as the fence label, like:\n"
        "```Code Box\n...your code...\n```\n"
        "2) When user asks to edit code, return only the updated code under Code Box with minimal explanation.\n"
        "3) When a user uploads files, analyze them by their file name and content, and use them to inform your response. Do not hallucinate file content.\n"
        "4) When the user asks for a project in a ZIP file, only respond with 'READY_FOR_ZIP' and nothing else.\n"
    )

    # Convert the list of uploaded files to the format expected by the API
    uploaded_files_list = []
    if file_info:
        for f in file_info:
            uploaded_files_list.append({
                "id": str(uuid.uuid4()),
                "name": f.get("name"),
                "content": f.get("content"),
                "mime": f.get("mime", "text/plain")
            })
    
    payload = {
        "aiInput": prompt,
        "token_type": "workik.openai:gpt_5_mini",
        "msg_type": "message",
        "uploaded_files": {"files": uploaded_files_list},
        "wk_ld": _current_tokens["wk_ld"],
        "wk_ck": _current_tokens["wk_ck"],
        "defaultContext": [
            {"id": str(uuid.uuid4()), "title":"Relevant Code","type":"code","codeFiles":{"files":[]},"uploadFiles":{"files":[]}},
            {"id": str(uuid.uuid4()), "title":"Your Database Schema","type":"tables","tables":[]},
            {"id": str(uuid.uuid4()), "title":"Rest API Designs","type":"request","requests":[]},
            {"id": str(uuid.uuid4()), "title":"Programming Language","type":"input","value_text": system_instructions},
            {"id": str(uuid.uuid4()), "title":"Relevant Packages","type":"checklist","options_list":[]}
        ],
        "editScript": {
            "id": str(uuid.uuid4()),
            "name": "My workspace",
            "messages": [
                {
                    "type": "question",
                    "responseType": "code" if is_edit_request else "text",
                    "sendTo": "ai",
                    "msg": prompt
                }
            ],
            "status": "own",
            "context": {}
        },
        "all_messages": [],
        "codingLanguage": "",
    }
    return payload

def is_edit_request(text):
    if not text:
        return False
    t = text.strip().lower()
    triggers = [
        "edit code", "code edit", "fix code", "refactor", "apply patch",
        "modify code", "update code", "change code", "edit:"
    ]
    return any(trigger in t for trigger in triggers)

def codebox_stream_wrapper(gen):
    """
    Transform any triple backtick fences to use ```Code Box for the opening fence.
    Keeps streaming behavior while handling boundary splits.
    """
    buffer = ""
    fence_open = False
    for chunk in gen:
        buffer += chunk
        while True:
            idx = buffer.find("```")
            if idx == -1:
                if not fence_open:
                    yield buffer
                    buffer = ""
                break
            before = buffer[:idx]
            after = buffer[idx + 3:]
            if not fence_open:
                nl = after.find("\n")
                if nl == -1:
                    break
                language = after[:nl]
                rest = after[nl + 1:]
                yield before
                yield "```Code Box\n"
                fence_open = True
                buffer = rest
            else:
                yield before + "```"
                fence_open = False
                buffer = after
    if buffer:
        yield buffer

def workik_stream(prompt, files=None, is_edit=False):
    payload = ai_payload(prompt, file_info=files, is_edit_request=is_edit)
    try:
        r = requests.post(API_URL, headers=headers, data=json.dumps(payload), stream=True, timeout=None)
    except Exception as e:
        yield f"Error: {str(e)}"
        return

    if r.status_code != 200:
        try:
            body = r.text
        except:
            body = ""
        yield f"Error: {r.status_code}, {body}"
        return

    for line in r.iter_lines(decode_unicode=True):
        if not line:
            continue
        try:
            data = json.loads(line)
            content = data.get("content")
            if content:
                yield content
        except Exception:
            continue

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/upload_files", methods=["POST"])
def upload_files():
    if "files" not in request.files:
        return jsonify({"error": "No files uploaded"}), 400
    
    uploaded_files = request.files.getlist("files")
    
    if len(uploaded_files) > 20:
        return jsonify({"error": "Maximum 20 files can be uploaded at once."}), 400
    
    results = []
    
    for f in uploaded_files:
        if f.filename.endswith(".zip"):
            try:
                with zipfile.ZipFile(f, 'r') as zip_ref:
                    extracted_files = []
                    for name in zip_ref.namelist():
                        # Skip directories, metadata files, and empty files
                        if not name.endswith('/') and '__MACOSX' not in name and zip_ref.getinfo(name).file_size > 0:
                            content = zip_ref.read(name).decode('utf-8')
                            mime_type, _ = mimetypes.guess_type(name)
                            if mime_type is None:
                                mime_type = 'text/plain'
                            extracted_files.append({
                                "name": name,
                                "content": content,
                                "mime": mime_type
                            })
                    results.extend(extracted_files)
            except zipfile.BadZipFile:
                return jsonify({"error": "Invalid or corrupted ZIP file"}), 400
            except UnicodeDecodeError:
                return jsonify({"error": f"One or more files in the ZIP archive are not valid text files and cannot be read."}), 400
            except Exception as e:
                return jsonify({"error": f"Error processing ZIP: {str(e)}"}), 500
        else:
            try:
                content = f.read().decode('utf-8')
                mime_type, _ = mimetypes.guess_type(f.filename)
                if mime_type is None:
                    mime_type = 'text/plain'
                results.append({
                    "name": f.filename,
                    "content": content,
                    "mime": mime_type
                })
            except UnicodeDecodeError:
                return jsonify({"error": f"File '{f.filename}' is not a valid text file"}), 400
            except Exception as e:
                return jsonify({"error": f"Error reading file '{f.filename}': {str(e)}"}), 500
    
    return jsonify(results), 200

@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(force=True, silent=True) or {}
    session = data.get("session") or str(uuid.uuid4())
    text = (data.get("text") or "").strip()
    action = data.get("action") or "chat"
    file_info_list = data.get("fileInfoList", [])
    is_edit = is_edit_request(text)

    # Handle continue
    if action == "continue":
        prev = session_last_prompt.get(session, "")
        if prev:
            text = prev + "\n\nContinue."
        else:
            text = "Continue."

    if action == "chat":
        if text:
            session_last_prompt[session] = text

    def generate():
        original_stream = workik_stream(text, files=file_info_list, is_edit=is_edit)
        transformed_stream = codebox_stream_wrapper(original_stream)
        for piece in transformed_stream:
            yield piece

    return Response(stream_with_context(generate()), mimetype="text/plain; charset=utf-8")

@app.route("/create_zip", methods=["POST"])
def create_zip():
    data = request.get_json(force=True, silent=True) or {}
    files_to_zip = data.get("files", [])
    
    if not files_to_zip:
        return jsonify({"error": "No files to zip"}), 400
    
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for file_content in files_to_zip:
            file_name = file_content.get('name')
            content = file_content.get('content')
            if file_name and content is not None:
                zip_file.writestr(file_name, content)
    
    zip_buffer.seek(0)
    return send_file(zip_buffer, mimetype='application/zip', as_attachment=True, download_name='gpt_project.zip')

# ====== Token refresh helpers (from your extraction code) ======
def extract_tokens_from_response(text):
    try:
        main_data = json.loads(text)
        if "request" in main_data and "post_data" in main_data["request"]:
            post_data_str = main_data["request"]["post_data"]
            post_data = json.loads(post_data_str)
            coding_language = post_data.get("codingLanguage")
            wk_ld = post_data.get("wk_ld")
            wk_ck = post_data.get("wk_ck")
            return coding_language, wk_ld, wk_ck
    except (json.JSONDecodeError, KeyError):
        pass
    
    coding_language_patterns = [
        r'"codingLanguage\\":\\"([^"\\]*(?:\\.[^"\\]*)*)\\"',
        r'"codingLanguage":"([^"]*)"',
        r'codingLanguage["\s]*:\s*["\']([^"\']*)["\']'
    ]
    wk_ld_patterns = [
        r'"wk_ld\\":\\"([^"\\]*(?:\\.[^"\\]*)*)\\"',
        r'"wk_ld":"([^"]*)"',
        r'wk_ld["\s]*:\s*["\']([^"\']*)["\']'
    ]
    wk_ck_patterns = [
        r'"wk_ck\\":\\"([^"\\]*(?:\\.[^"\\]*)*)\\"',
        r'"wk_ck":"([^"]*)"',
        r'wk_ck["\s]*:\s*["\']([^"\']*)["\']'
    ]
    
    def find_first(patterns, text_):
        for p in patterns:
            m = re.search(p, text_)
            if m:
                return m.group(1)
        return None

    coding_language = find_first(coding_language_patterns, text)
    wk_ld = find_first(wk_ld_patterns, text)
    wk_ck = find_first(wk_ck_patterns, text)
    return coding_language, wk_ld, wk_ck

@app.route("/refresh_tokens", methods=["GET"])
def refresh_tokens():
    url = "https://host-2-iyfn.onrender.com/run-task2"
    try:
        # Use timeout=None as requested for indefinite wait
        res = requests.get(url, timeout=None)
        res.raise_for_status()
        text = res.text.strip()
        coding_language, wk_ld, wk_ck = extract_tokens_from_response(text)
        if not wk_ld or not wk_ck:
            return jsonify({"error": "Failed to extract tokens from API response. Check the raw response for errors."}), 500
        return jsonify({
            "codingLanguage": coding_language,
            "wk_ld": wk_ld,
            "wk_ck": wk_ck
        }), 200
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Request to token API failed: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": f"An unexpected error occurred during token refresh: {str(e)}"}), 500

@app.route("/apply_tokens", methods=["POST"])
def apply_tokens():
    global _current_tokens
    data = request.get_json(force=True, silent=True) or {}
    wk_ld = data.get("wk_ld")
    wk_ck = data.get("wk_ck")
    coding_language = data.get("codingLanguage")
    if not wk_ld or not wk_ck:
        return jsonify({"error": "wk_ld and wk_ck required"}), 400

    _current_tokens["wk_ld"] = wk_ld
    _current_tokens["wk_ck"] = wk_ck
    append_tokens_to_file(wk_ld, wk_ck, coding_language=coding_language)
    return jsonify({"ok": True}), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)