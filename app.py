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

def ai_payload(prompt, messages=None, file_info=None, is_edit_request=False):
    # System instructions for the AI
    system_instructions = (
        "You are Gpt 5 mini. Rules:\n"
        "1) Always place any code inside triple backticks with 'Code Box' as the language identifier, like:\n"
        "```Code Box\n...your code...\n```\n"
        "2) When a user asks to edit code, return only the updated code under a 'Code Box' with minimal explanation.\n"
        "3) When a user uploads files, analyze them by their file name and content, and use them to inform your response. Do not hallucinate file content.\n"
        "4) When the user asks for a project in a ZIP file, only respond with 'READY_FOR_ZIP' and nothing else.\n"
        "5) You have a memory of the current conversation. Use the provided message history to understand context and follow-up questions."
    )

    uploaded_files_list = []
    if file_info:
        for f in file_info:
            uploaded_files_list.append({
                "id": str(uuid.uuid4()),
                "name": f.get("name"),
                "content": f.get("content"),
                "mime": f.get("mime", "text/plain")
            })
    
    # Ensure messages are in the correct format for the API
    api_messages = []
    if messages:
        for msg in messages:
            # The API might expect 'assistant' instead of 'bot'
            role = "assistant" if msg.get("role") == "bot" else msg.get("role")
            api_messages.append({"role": role, "content": msg.get("content")})

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
        "all_messages": api_messages, # <-- **MEMORY FIX**: Pass the conversation history
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

def workik_stream(prompt, messages=None, files=None, is_edit=False):
    payload = ai_payload(prompt, messages=messages, file_info=files, is_edit_request=is_edit)
    try:
        r = requests.post(API_URL, headers=headers, data=json.dumps(payload), stream=True, timeout=None)
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        yield f"Error: Could not connect to the AI service. Details: {str(e)}"
        return

    for line in r.iter_lines(decode_unicode=True):
        if not line:
            continue
        try:
            # The API often sends 'data: ' prefix
            if line.startswith('data: '):
                line = line[6:]
            data = json.loads(line)
            content = data.get("content")
            if content:
                yield content
        except (json.JSONDecodeError, AttributeError):
            # Ignore lines that are not valid JSON or don't have the expected structure
            continue

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/upload_files", methods=["POST"])
def upload_files():
    try:
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
                            if not name.endswith('/') and '__MACOSX' not in name and zip_ref.getinfo(name).file_size > 0:
                                content = zip_ref.read(name).decode('utf-8')
                                mime_type, _ = mimetypes.guess_type(name)
                                extracted_files.append({
                                    "name": name,
                                    "content": content,
                                    "mime": mime_type or 'text/plain'
                                })
                        results.extend(extracted_files)
                except zipfile.BadZipFile:
                    return jsonify({"error": "Invalid or corrupted ZIP file"}), 400
                except UnicodeDecodeError:
                    return jsonify({"error": f"Files in the ZIP must be valid text (UTF-8)."}), 400
                except Exception as e:
                    return jsonify({"error": f"Error processing ZIP: {str(e)}"}), 500
            else:
                try:
                    content = f.read().decode('utf-8')
                    mime_type, _ = mimetypes.guess_type(f.filename)
                    results.append({
                        "name": f.filename,
                        "content": content,
                        "mime": mime_type or 'text/plain'
                    })
                except UnicodeDecodeError:
                    return jsonify({"error": f"File '{f.filename}' is not a valid text file"}), 400
                except Exception as e:
                    return jsonify({"error": f"Error reading file '{f.filename}': {str(e)}"}), 500
        
        return jsonify(results), 200
    except Exception as e:
        # A catch-all for any unexpected errors during file processing
        return jsonify({"error": f"An unexpected server error occurred: {str(e)}"}), 500


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    messages = data.get("messages", []) # <-- **MEMORY FIX**: Get history from client
    file_info_list = data.get("fileInfoList", [])
    is_edit = is_edit_request(text)

    def generate():
        # The stream is now sent directly to the client without the complex wrapper
        for piece in workik_stream(text, messages=messages, files=file_info_list, is_edit=is_edit):
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
            return (
                post_data.get("codingLanguage"),
                post_data.get("wk_ld"),
                post_data.get("wk_ck")
            )
    except (json.JSONDecodeError, KeyError):
        pass
    
    patterns = {
        "codingLanguage": [r'"codingLanguage\\":\\"([^"\\]*(?:\\.[^"\\]*)*)\\"', r'"codingLanguage":"([^"]*)"'],
        "wk_ld": [r'"wk_ld\\":\\"([^"\\]*(?:\\.[^"\\]*)*)\\"', r'"wk_ld":"([^"]*)"'],
        "wk_ck": [r'"wk_ck\\":\\"([^"\\]*(?:\\.[^"\\]*)*)\\"', r'"wk_ck":"([^"]*)"']
    }
    
    def find_first(key, text_):
        for p in patterns[key]:
            m = re.search(p, text_)
            if m: return m.group(1)
        return None

    return (
        find_first("codingLanguage", text),
        find_first("wk_ld", text),
        find_first("wk_ck", text)
    )

@app.route("/refresh_tokens", methods=["GET"])
def refresh_tokens():
    url = "https://host-2-iyfn.onrender.com/run-task2"
    try:
        res = requests.get(url, timeout=120) # Added a reasonable timeout
        res.raise_for_status()
        text = res.text.strip()
        coding_language, wk_ld, wk_ck = extract_tokens_from_response(text)
        if not wk_ld or not wk_ck:
            return jsonify({"error": "Failed to extract tokens from API response."}), 500
        return jsonify({
            "codingLanguage": coding_language,
            "wk_ld": wk_ld,
            "wk_ck": wk_ck
        }), 200
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Request to token API failed: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": f"An unexpected error occurred: {str(e)}"}), 500

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
