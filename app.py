import os
import json
import uuid
import requests
import datetime
import re
import zipfile
import io
import mimetypes
import docker
import threading
import time
import tempfile
import shutil
from flask import Flask, request, Response, jsonify, send_from_directory, stream_with_context, send_file
from flask_socketio import SocketIO, emit, join_room, leave_room
import subprocess
import signal

app = Flask(__name__, static_url_path="", static_folder=".")
app.config['SECRET_KEY'] = 'your-secret-key'
socketio = SocketIO(app, cors_allowed_origins="*")

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

# ====== Docker & Terminal Management ======
try:
    docker_client = docker.from_env()
except:
    docker_client = None

active_containers = {}
active_terminals = {}
preview_urls = {}

class TerminalManager:
    def __init__(self, container_id):
        self.container_id = container_id
        self.container = docker_client.containers.get(container_id)
        self.exec_id = None
        self.process = None
        
    def execute_command(self, command, room_id):
        try:
            exec_result = self.container.exec_run(
                command, 
                tty=True, 
                stdin=True, 
                stdout=True, 
                stderr=True,
                stream=True,
                socket=True
            )
            
            for chunk in exec_result.output:
                if chunk:
                    output = chunk.decode('utf-8', errors='ignore')
                    socketio.emit('terminal_output', {
                        'output': output,
                        'type': 'stdout'
                    }, room=room_id)
                    
        except Exception as e:
            socketio.emit('terminal_output', {
                'output': f"Error executing command: {str(e)}\n",
                'type': 'stderr'
            }, room=room_id)

def create_code_container(session_id):
    """Create a new Docker container for code execution"""
    if not docker_client:
        return None
        
    try:
        # Create container with Ubuntu and common development tools
        container = docker_client.containers.run(
            "ubuntu:22.04",
            command="bash",
            detach=True,
            tty=True,
            stdin_open=True,
            working_dir="/workspace",
            volumes={tempfile.gettempdir(): {'bind': '/tmp', 'mode': 'rw'}},
            name=f"code_executor_{session_id}",
            remove=True,
            mem_limit="512m",
            cpu_period=100000,
            cpu_quota=50000
        )
        
        # Install basic tools
        setup_commands = [
            "apt-get update",
            "apt-get install -y python3 python3-pip nodejs npm curl wget unzip git",
            "apt-get install -y build-essential",
            "mkdir -p /workspace",
            "cd /workspace"
        ]
        
        for cmd in setup_commands:
            container.exec_run(cmd, detach=False)
            
        active_containers[session_id] = container.id
        active_terminals[session_id] = TerminalManager(container.id)
        
        # Set expiry timer (5 minutes)
        timer = threading.Timer(300, cleanup_container, args=[session_id])
        timer.start()
        
        return container.id
        
    except Exception as e:
        print(f"Error creating container: {e}")
        return None

def cleanup_container(session_id):
    """Clean up container and related resources"""
    try:
        if session_id in active_containers:
            container = docker_client.containers.get(active_containers[session_id])
            container.stop(timeout=5)
            del active_containers[session_id]
            
        if session_id in active_terminals:
            del active_terminals[session_id]
            
        if session_id in preview_urls:
            del preview_urls[session_id]
            
    except Exception as e:
        print(f"Error cleaning up container: {e}")

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
                return code_blocks[-1]
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

def should_show_run_button(content):
    """Check if the response contains code that can be executed"""
    if not content:
        return False
        
    # Check for code blocks
    has_code_block = '```Code Box' in content or '```html' in content or '```python' in content or '```javascript' in content
    
    # Check for web-related keywords
    web_keywords = ['html', 'css', 'javascript', 'website', 'web page', 'frontend', 'backend', 'server', 'app']
    has_web_content = any(keyword in content.lower() for keyword in web_keywords)
    
    return has_code_block and (has_web_content or 'python' in content.lower() or 'node' in content.lower())

def ai_payload(prompt, messages=None, file_info=None, is_edit_request=False, is_continue=False, last_code=None):
    # Enhanced system instructions with terminal capabilities
    system_instructions = (
        "You are Gpt 5 mini with terminal access. Rules:\n"
        "1) Always place any code inside triple backticks with 'Code Box' as the language identifier, like:\n"
        "```Code Box\n...your code...\n```\n"
        "2) When a user asks to edit code, take the provided existing code and modify it according to their request. "
        "Return only the complete updated code under a 'Code Box' with minimal explanation.\n"
        "3) When editing code, maintain the existing structure and add/modify only what's requested.\n"
        "4) When a user uploads files, analyze them by their file name and content, and use them to inform your response.\n"
        "5) When the user asks for a project in a ZIP file, only respond with 'READY_FOR_ZIP' and nothing else.\n"
        "6) You have a memory of the current conversation. Use the provided message history to understand context.\n"
        "7) If this is a continue request, continue from where you left off without repeating content.\n"
        "8) Focus on clean, efficient, and well-commented code.\n"
        "9) Always provide complete, working code solutions.\n"
        "10) When providing code that can be executed (web apps, Python scripts, etc.), mention that it can be run using the Run Code button.\n"
        "11) If you need to install packages or debug errors, provide the exact terminal commands needed.\n"
        "12) For web applications, always include proper HTML structure with CSS and JavaScript if needed."
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

# ====== Socket.IO Events ======
@socketio.on('connect')
def handle_connect():
    print('Client connected')

@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')

@socketio.on('join_terminal')
def handle_join_terminal(data):
    session_id = data.get('session_id')
    join_room(session_id)
    emit('terminal_status', {'status': 'connected', 'session_id': session_id})

@socketio.on('terminal_command')
def handle_terminal_command(data):
    session_id = data.get('session_id')
    command = data.get('command')
    
    if session_id in active_terminals:
        terminal = active_terminals[session_id]
        # Execute command in background thread
        thread = threading.Thread(target=terminal.execute_command, args=(command, session_id))
        thread.start()
    else:
        emit('terminal_output', {
            'output': 'Error: Terminal session not found\n',
            'type': 'stderr'
        }, room=session_id)

# ====== HTTP Routes ======
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
                        # Add zip info for terminal processing
                        results.append({
                            "name": "__ZIP_INFO__",
                            "content": f"Extracted {len(extracted_files)} files from {f.filename}",
                            "mime": "text/plain",
                            "is_zip": True
                        })
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
        bot_response = ""
        for piece in workik_stream(text, messages=messages, files=file_info_list, is_edit=is_edit, is_continue=is_continue, last_code=last_code):
            bot_response += piece
            yield piece
        
        # Check if response should have run button
        if should_show_run_button(bot_response):
            yield "\n\n__SHOW_RUN_BUTTON__"

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
        bot_response = ""
        for piece in workik_stream(text, messages=regenerate_messages, files=file_info_list, is_edit=is_edit, last_code=last_code):
            bot_response += piece
            yield piece
        
        # Check if response should have run button
        if should_show_run_button(bot_response):
            yield "\n\n__SHOW_RUN_BUTTON__"

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

@app.route("/run_code", methods=["POST"])
def run_code():
    """Create a new code execution environment"""
    data = request.get_json(force=True, silent=True) or {}
    code = data.get("code", "")
    session_id = data.get("session_id") or str(uuid.uuid4())
    file_list = data.get("files", [])
    
    if not docker_client:
        return jsonify({"error": "Docker not available"}), 500
    
    try:
        # Create container
        container_id = create_code_container(session_id)
        if not container_id:
            return jsonify({"error": "Failed to create execution environment"}), 500
        
        # Generate preview URL (this would be your domain + session_id)
        preview_url = f"https://{request.host}/preview/{session_id}"
        preview_urls[session_id] = {
            "url": preview_url,
            "created_at": time.time(),
            "expires_at": time.time() + 300  # 5 minutes
        }
        
        return jsonify({
            "success": True,
            "session_id": session_id,
            "container_id": container_id,
            "preview_url": preview_url,
            "expires_in": 300
        })
        
    except Exception as e:
        return jsonify({"error": f"Failed to create execution environment: {str(e)}"}), 500

@app.route("/preview/<session_id>")
def preview_code(session_id):
    """Serve the code preview"""
    if session_id not in preview_urls:
        return "Preview session not found or expired", 404
    
    preview_info = preview_urls[session_id]
    if time.time() > preview_info["expires_at"]:
        cleanup_container(session_id)
        return "Preview session expired", 410
    
    # Serve index.html from the container if it exists
    try:
        if session_id in active_containers:
            container = docker_client.containers.get(active_containers[session_id])
            # Try to get index.html content
            try:
                exec_result = container.exec_run("cat /workspace/index.html")
                if exec_result.exit_code == 0:
                    return exec_result.output.decode('utf-8')
            except:
                pass
                
            # Fallback to simple HTML
            return """
            <html>
            <head><title>Code Preview</title></head>
            <body>
                <h1>Code Preview</h1>
                <p>Your code is running in the terminal. Check the chat for output.</p>
                <script>
                    // Auto-refresh every 2 seconds
                    setTimeout(() => location.reload(), 2000);
                </script>
            </body>
            </html>
            """
    except:
        pass
    
    return "Error loading preview", 500

# ====== Token refresh helpers ======
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

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
