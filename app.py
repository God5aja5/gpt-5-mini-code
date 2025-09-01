import os
import json
import uuid
import requests
import datetime
import re
import zipfile
import io
import mimetypes
import subprocess
import threading
import time
import queue

from flask import Flask, request, Response, jsonify, send_from_directory, stream_with_context, send_file
from werkzeug.serving import is_running_from_reloader

app = Flask(__name__, static_url_path="", static_folder=".")

# ====== Workik API Setup (GPT-5 mini only) ======
API_URL = "https://wfhbqniijcsamdp2v3i6nts4ke0ebkyj.lambda-url.us-east-1.on.aws/api_ai_playground/ai/playground/ai/trigger"
WORKIK_TOKEN = os.getenv("WORKIK_TOKEN", "undefined")

# Default tokens (will be overridden by tokens.txt if present)
DEFAULT_WK_LD = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0eXBlIjoibG9jYWwiLCJzZXNzaW9uX2lkIjoiMTc1NjM4ODE4MyIsInJlcXVlc3RfY291bnQiOjAsImV4cCI6MTc1Njk5Mjk4M30.JbAEBmTbWtyysFGDftxRJvqy1LvJfIR1W-HVv_Ss-7U"
DEFAULT_WK_CK = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0eXBlIjoiY29va2llIiwic2Vzc2lvbl9pZCI6IjE3NTYzODgxODMiLCJyZXF1ZXN0X2NvdW50OjAsImV4cCI6MTc1Njk5Mjk4M30.Fua9aRmHJLPte8cF807w4jHA6Ff1GPwGAaOWcY9P7Us"

TOKENS_FILE = "tokens.txt"
PROJECTS_DIR = os.path.join(os.getcwd(), "temp_projects")
if not os.path.exists(PROJECTS_DIR):
    os.makedirs(PROJECTS_DIR)

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

# In-memory session data for terminals
terminal_sessions = {}
session_lock = threading.Lock()

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

def extract_last_code_block(messages):
    """Extract the most recent code block from bot messages"""
    for msg in reversed(messages):
        if msg.get("role") == "bot" and msg.get("content"):
            code_blocks = re.findall(r'```Code Box\n([\s\S]*?)\n```', msg["content"])
            if code_blocks:
                return code_blocks[-1]  # Return the last code block
    return None

def is_code_edit_request(text, has_previous_code=False):
    """Check if the user is requesting to edit existing code"""
    if not text:
        return False
    
    t = text.strip().lower()
    
    # Strong edit indicators
    strong_edit_triggers = [
        "edit the code", "modify the code", "update the code", "change the code",
        "add to the code", "edit this", "modify this", "update this",
        "add this feature", "add this functionality", "improve the code",
        "fix the code", "refactor the code", "optimize the code"
    ]
    
    # Weaker indicators that need previous code context
    weak_edit_triggers = [
        "add", "include", "also add", "now add", "please add",
        "integrate", "implement", "enhance", "extend"
    ]
    
    # Check strong indicators first
    for trigger in strong_edit_triggers:
        if trigger in t:
            return True
    
    # Check weak indicators only if there's previous code
    if has_previous_code:
        for trigger in weak_edit_triggers:
            if trigger in t and any(word in t for word in ["function", "feature", "method", "class", "variable", "property", "support", "functionality"]):
                return True
    
    return False

def ai_payload(prompt, messages=None, file_info=None, is_edit_request=False, is_continue=False, last_code=None):
    # Enhanced system instructions
    system_instructions = (
        "You are Gpt 5 mini. Rules:\n"
        "1) Always place any code inside triple backticks with 'Code Box' as the language identifier, like:\n"
        "```Code Box\n...your code...\n```\n"
        "2) When a user asks to edit code, take the provided existing code and modify it according to their request. "
        "Return only the complete updated code under a 'Code Box' with minimal explanation.\n"
        "3) When editing code, maintain the existing structure and add/modify only what's requested.\n"
        "4) When a user uploads files, analyze them by their file name and content, and use them to inform your response. "
        "Also, create a detailed `PROJECT_FILE_STRUCTURE` in markdown to show the file tree after unzipping.\n"
        "5) When the user asks for a project in a ZIP file, only respond with 'READY_FOR_ZIP' and nothing else.\n"
        "6) You have a memory of the current conversation. Use the provided message history to understand context.\n"
        "7) If this is a continue request, continue from where you left off without repeating content.\n"
        "8) Focus on clean, efficient, and well-commented code.\n"
        "9) When asked to run code, provide a run command inside a `<RUN_CMD>` tag like `<RUN_CMD>python main.py</RUN_CMD>`. If you see a user request that implies running or executing code, you MUST respond with the run command. You can include multiple commands. For websites, you must also provide a `<PREVIEW_URL>`. After providing the run command, you will start a terminal session. You will provide real-time updates from the terminal, prefixed with `-> SHELL OUTPUT:`. If there is an error, you must debug and provide a fix. After the fix, you should provide the corrected run command again.\n"
        "10) ALWAYS provide complete, working code solutions. If you need to install packages, provide the `pip install` or `npm install` command first inside `<RUN_CMD>`.\n"
        "11) Always provide code in a single, complete block unless you are being asked to provide multiple files. In that case, use clear filename comments like `// file: filename.js` at the top of each block.\n"
    )

    # If it's an edit request and we have previous code, include it in the context
    if is_edit_request and last_code:
        enhanced_prompt = f"Here's the existing code:\n\n```Code Box\n{last_code}\n```\n\nUser request: {prompt}\n\nPlease edit the above code according to the user's request and return the complete updated code."
        prompt = enhanced_prompt

    # If it's a continue request, add continue instruction
    if is_continue:
        prompt = f"Continue from where you left off: {prompt}"

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
            content = msg.get("content")
            if msg.get("role") == "user" and msg.get("files"):
                # Append file info to user message content for context
                file_names = ", ".join([f['name'] for f in msg['files']])
                content += f"\n\n[Uploaded files: {file_names}]"
            api_messages.append({"role": role, "content": content})

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
        "all_messages": api_messages,
        "codingLanguage": "",
    }
    return payload

def workik_stream(prompt, messages=None, files=None, is_edit=False, is_continue=False, last_code=None):
    payload = ai_payload(prompt, messages=messages, file_info=files, is_edit_request=is_edit, is_continue=is_continue, last_code=last_code)
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
    messages = data.get("messages", [])
    file_info_list = data.get("fileInfoList", [])
    is_continue = data.get("isContinue", False)
    
    # Extract last code block for edit detection
    last_code = extract_last_code_block(messages)
    is_edit = is_code_edit_request(text, has_previous_code=bool(last_code))

    def generate():
        for piece in workik_stream(text, messages=messages, files=file_info_list, is_edit=is_edit, is_continue=is_continue, last_code=last_code):
            yield piece

    return Response(stream_with_context(generate()), mimetype="text/plain; charset=utf-8")

@app.route("/regenerate", methods=["POST"])
def regenerate():
    """Regenerate the last bot response"""
    data = request.get_json(force=True, silent=True) or {}
    messages = data.get("messages", [])
    
    if not messages or messages[-1].get("role") != "bot":
        return jsonify({"error": "No bot message to regenerate"}), 400
    
    # Get the user message that triggered the bot response
    user_messages = [msg for msg in messages if msg.get("role") == "user"]
    if not user_messages:
        return jsonify({"error": "No user message found"}), 400
    
    last_user_message = user_messages[-1]
    # Remove the last bot message from history for regeneration
    regenerate_messages = messages[:-1]
    
    text = last_user_message.get("content", "")
    file_info_list = last_user_message.get("files", [])
    
    # Extract last code block for edit detection
    last_code = extract_last_code_block(regenerate_messages)
    is_edit = is_code_edit_request(text, has_previous_code=bool(last_code))

    def generate():
        for piece in workik_stream(text, messages=regenerate_messages, files=file_info_list, is_edit=is_edit, last_code=last_code):
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
        "codingLanguage": [r'"codingLanguage\\":\\"([^"\```*(?:\\.[^"\```*)*)\\"', r'"codingLanguage":"([^"]*)"'],
        "wk_ld": [r'"wk_ld\\":\\"([^"\```*(?:\\.[^"\```*)*)\\"', r'"wk_ld":"([^"]*)"'],
        "wk_ck": [r'"wk_ck\\":\\"([^"\```*(?:\\.[^"\```*)*)\\"', r'"wk_ck":"([^"]*)"']
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
        res = requests.get(url, timeout=120)
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

# ====== New Terminal and Preview Feature ======
def find_free_port(start_port=5001, end_port=6000):
    for port in range(start_port, end_port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except socket.error:
                continue
    return None

def start_shell_session(session_id, project_path):
    with session_lock:
        if session_id in terminal_sessions:
            return terminal_sessions[session_id]

        output_queue = queue.Queue()
        is_running = True
        preview_port = find_free_port()
        if not preview_port:
            output_queue.put("Error: Could not find a free port to serve the application.")
            return None, None

        def monitor_process(process, q, timeout=300):
            try:
                end_time = time.time() + timeout
                while time.time() < end_time and process.poll() is None:
                    line = process.stdout.readline()
                    if line:
                        q.put(line.decode('utf-8'))
                    time.sleep(0.1)
                if process.poll() is None:
                    process.terminate()
                    q.put("\nSession timeout (5 minutes). Terminating process.")
            except Exception as e:
                q.put(f"\nAn error occurred in the terminal process: {e}")
            finally:
                is_running = False
                q.put(None)  # Signal that the process is done

        def read_pipe(pipe, q):
            for line in iter(pipe.readline, b''):
                q.put(line.decode('utf-8'))
            pipe.close()
            
        process = subprocess.Popen(
            ['/bin/bash'],
            cwd=project_path,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        
        thread = threading.Thread(target=monitor_process, args=(process, output_queue, 300))
        thread.daemon = True
        thread.start()

        terminal_sessions[session_id] = {
            "process": process,
            "output_queue": output_queue,
            "thread": thread,
            "project_path": project_path,
            "created_at": time.time(),
            "preview_url": f"http://localhost:{preview_port}"
        }
        
        return process, output_port

def run_command(session_id, command):
    with session_lock:
        session = terminal_sessions.get(session_id)
        if not session or session['process'].poll() is not None:
            return {"error": "Terminal session not active."}
        
        try:
            session['process'].stdin.write(command + '\n')
            session['process'].stdin.flush()
            return {"success": True}
        except Exception as e:
            return {"error": str(e)}

@app.route("/terminal/<session_id>/output", methods=["GET"])
def stream_terminal_output(session_id):
    def generate():
        with session_lock:
            session = terminal_sessions.get(session_id)
            if not session:
                yield "Error: No active terminal session found.\n"
                return
        
        q = session['output_queue']
        while session['thread'].is_alive() or not q.empty():
            try:
                line = q.get(timeout=1)
                if line is None:
                    break
                yield line
            except queue.Empty:
                continue

    return Response(stream_with_context(generate()), mimetype="text/plain")

@app.route("/terminal/<session_id>/command", methods=["POST"])
def send_terminal_command(session_id):
    data = request.get_json(force=True, silent=True) or {}
    command = data.get("command")
    if not command:
        return jsonify({"error": "Command not provided."}), 400
    
    result = run_command(session_id, command)
    return jsonify(result)

def cleanup_old_sessions():
    with session_lock:
        now = time.time()
        to_delete = [sid for sid, session in terminal_sessions.items() if now - session["created_at"] > 300 or session["process"].poll() is not None]
        for sid in to_delete:
            print(f"Cleaning up session: {sid}")
            session = terminal_sessions[sid]
            if session["process"].poll() is None:
                session["process"].terminate()
            if os.path.exists(session["project_path"]):
                shutil.rmtree(session["project_path"])
            del terminal_sessions[sid]

@app.before_request
def before_request_hook():
    if not is_running_from_reloader():
        # Only run cleanup in the main process, not the reloader
        cleanup_old_sessions()
        
if __name__ == "__main__":
    import socket
    import shutil
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
