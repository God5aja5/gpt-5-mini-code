import os
import json
import uuid
import requests
import datetime
import re
import zipfile
import io
import mimetypes
import threading
import time
import tempfile
import shutil
from flask import Flask, request, Response, jsonify, send_from_directory, stream_with_context, send_file
from flask_socketio import SocketIO, emit, join_room, leave_room
import subprocess
import signal
import logging

app = Flask(__name__, static_url_path="", static_folder=".")
app.config['SECRET_KEY'] = 'your-secret-key-here'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ====== Workik API Setup (GPT-5 mini only) ======
API_URL = "https://wfhbqniijcsamdp2v3i6nts4ke0ebkyj.lambda-url.us-east-1.on.aws/api_ai_playground/ai/playground/ai/trigger"
WORKIK_TOKEN = os.getenv("WORKIK_TOKEN", "undefined")

# Default tokens
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

# ====== Container Management (using subprocess instead of Docker) ======
active_containers = {}
active_terminals = {}
preview_urls = {}
container_outputs = {}

class TerminalManager:
    def __init__(self, session_id):
        self.session_id = session_id
        self.workspace_dir = f"/tmp/workspace_{session_id}"
        os.makedirs(self.workspace_dir, exist_ok=True)
        self.current_process = None
        
    def execute_command(self, command, room_id):
        try:
            # Change to workspace directory
            os.chdir(self.workspace_dir)
            
            # Execute command
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1
            )
            
            # Stream output in real-time
            for line in iter(process.stdout.readline, ''):
                if line:
                    socketio.emit('terminal_output', {
                        'output': line,
                        'type': 'stdout'
                    }, room=room_id)
                    
                    # Store output for preview
                    if self.session_id not in container_outputs:
                        container_outputs[self.session_id] = []
                    container_outputs[self.session_id].append(line)
            
            process.stdout.close()
            return_code = process.wait()
            
            if return_code != 0:
                socketio.emit('terminal_output', {
                    'output': f"Command exited with code {return_code}\n",
                    'type': 'stderr'
                }, room=room_id)
                    
        except Exception as e:
            socketio.emit('terminal_output', {
                'output': f"Error executing command: {str(e)}\n",
                'type': 'stderr'
            }, room=room_id)

def create_code_container(session_id):
    """Create a new execution environment"""
    try:
        workspace_dir = f"/tmp/workspace_{session_id}"
        os.makedirs(workspace_dir, exist_ok=True)
        
        # Install basic tools
        setup_commands = [
            "which python3 || (apt-get update && apt-get install -y python3 python3-pip)",
            "which node || (curl -fsSL https://deb.nodesource.com/setup_18.x | bash - && apt-get install -y nodejs)",
            "which git || apt-get install -y git curl wget unzip"
        ]
        
        for cmd in setup_commands:
            try:
                subprocess.run(cmd, shell=True, cwd=workspace_dir, timeout=60)
            except:
                pass
        
        active_terminals[session_id] = TerminalManager(session_id)
        container_outputs[session_id] = []
        
        # Set expiry timer (5 minutes)
        timer = threading.Timer(300, cleanup_container, args=[session_id])
        timer.start()
        
        return session_id
        
    except Exception as e:
        logger.error(f"Error creating container: {e}")
        return None

def cleanup_container(session_id):
    """Clean up container and related resources"""
    try:
        if session_id in active_terminals:
            workspace_dir = f"/tmp/workspace_{session_id}"
            if os.path.exists(workspace_dir):
                shutil.rmtree(workspace_dir, ignore_errors=True)
            del active_terminals[session_id]
            
        if session_id in preview_urls:
            del preview_urls[session_id]
            
        if session_id in container_outputs:
            del container_outputs[session_id]
            
    except Exception as e:
        logger.error(f"Error cleaning up container: {e}")

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
    
    strong_edit_triggers = [
        "edit the code", "modify the code", "update the code", "change the code",
        "add to the code", "edit this", "modify this", "update this",
        "add this feature", "add this functionality", "improve the code",
        "fix the code", "refactor the code", "optimize the code"
    ]
    
    weak_edit_triggers = [
        "add", "include", "also add", "now add", "please add",
        "integrate", "implement", "enhance", "extend"
    ]
    
    for trigger in strong_edit_triggers:
        if trigger in t:
            return True
    
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
    has_code_block = any(marker in content for marker in [
        '```Code Box', '```html', '```python', '```javascript', 
        '```js', '```py', '```java', '```cpp', '```c++'
    ])
    
    # Check for programming keywords
    programming_keywords = [
        'def ', 'import ', 'print(', 'if __name__', 'python',
        'function ', 'const ', 'let ', 'var ', 'console.log', 'require(',
        'html', 'css', 'javascript', 'website', 'web page', 'frontend', 'backend',
        'code', 'script', 'program', 'execute', 'run', 'flask', 'django', 'fastapi',
        'express', 'node', 'react', 'vue', 'angular'
    ]
    
    content_lower = content.lower()
    has_programming_content = any(keyword in content_lower for keyword in programming_keywords)
    
    return has_code_block or has_programming_content

def ai_payload(prompt, messages=None, file_info=None, is_edit_request=False, is_continue=False, last_code=None):
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
        "12) For web applications, always include proper HTML structure with CSS and JavaScript if needed.\n"
        "13) For Python apps, include all necessary imports and proper error handling.\n"
        "14) For Node.js apps, include package.json and proper dependencies."
    )

    if is_edit_request and last_code:
        enhanced_prompt = f"Here's the existing code:\n\n```Code Box\n{last_code}\n```\n\nUser request: {prompt}\n\nPlease edit the above code according to the user's request and return the complete updated code."
        prompt = enhanced_prompt

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
    
    api_messages = []
    if messages:
        for msg in messages:
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
            if line.startswith('data: '):
                line = line[6:]
            data = json.loads(line)
            content = data.get("content")
            if content:
                yield content
        except (json.JSONDecodeError, AttributeError):
            continue

# ====== Socket.IO Events ======
@socketio.on('connect')
def handle_connect():
    logger.info('Client connected')

@socketio.on('disconnect')
def handle_disconnect():
    logger.info('Client disconnected')

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
        thread = threading.Thread(target=terminal.execute_command, args=(command, session_id))
        thread.daemon = True
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
                                try:
                                    content = zip_ref.read(name).decode('utf-8')
                                    mime_type, _ = mimetypes.guess_type(name)
                                    extracted_files.append({
                                        "name": name,
                                        "content": content,
                                        "mime": mime_type or 'text/plain'
                                    })
                                except UnicodeDecodeError:
                                    # Skip binary files
                                    continue
                        results.extend(extracted_files)
                        results.append({
                            "name": "__ZIP_INFO__",
                            "content": f"Extracted {len(extracted_files)} files from {f.filename}",
                            "mime": "text/plain",
                            "is_zip": True
                        })
                except zipfile.BadZipFile:
                    return jsonify({"error": "Invalid or corrupted ZIP file"}), 400
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
    
    last_code = extract_last_code_block(messages)
    is_edit = is_code_edit_request(text, has_previous_code=bool(last_code))

    def generate():
        bot_response = ""
        for piece in workik_stream(text, messages=messages, files=file_info_list, is_edit=is_edit, is_continue=is_continue, last_code=last_code):
            bot_response += piece
            yield piece
        
        if should_show_run_button(bot_response):
            yield "\n\n__SHOW_RUN_BUTTON__"

    return Response(stream_with_context(generate()), mimetype="text/plain; charset=utf-8")

@app.route("/regenerate", methods=["POST"])
def regenerate():
    data = request.get_json(force=True, silent=True) or {}
    messages = data.get("messages", [])
    
    if not messages or messages[-1].get("role") != "bot":
        return jsonify({"error": "No bot message to regenerate"}), 400
    
    user_messages = [msg for msg in messages if msg.get("role") == "user"]
    if not user_messages:
        return jsonify({"error": "No user message found"}), 400
    
    last_user_message = user_messages[-1]
    regenerate_messages = messages[:-1]
    
    text = last_user_message.get("content", "")
    file_info_list = last_user_message.get("files", [])
    
    last_code = extract_last_code_block(regenerate_messages)
    is_edit = is_code_edit_request(text, has_previous_code=bool(last_code))

    def generate():
        bot_response = ""
        for piece in workik_stream(text, messages=regenerate_messages, files=file_info_list, is_edit=is_edit, last_code=last_code):
            bot_response += piece
            yield piece
        
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
    data = request.get_json(force=True, silent=True) or {}
    code = data.get("code", "")
    session_id = data.get("session_id") or str(uuid.uuid4())
    file_list = data.get("files", [])
    
    try:
        container_id = create_code_container(session_id)
        if not container_id:
            return jsonify({"error": "Failed to create execution environment"}), 500
        
        base_url = "https://ng-x-sukuna-coder.onrender.com"  # Your Render URL
        preview_url = f"{base_url}/preview/{session_id}"
        
        preview_urls[session_id] = {
            "preview_url": preview_url,
            "created_at": time.time(),
            "expires_at": time.time() + 300
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
@app.route("/preview/<session_id>/<path:path>")
def preview_code(session_id, path=""):
    if session_id not in preview_urls:
        return "Preview session not found or expired", 404
    
    preview_info = preview_urls[session_id]
    if time.time() > preview_info["expires_at"]:
        cleanup_container(session_id)
        return "Preview session expired", 410
    
    workspace_dir = f"/tmp/workspace_{session_id}"
    
    if path:
        file_path = os.path.join(workspace_dir, path)
        if os.path.exists(file_path) and os.path.isfile(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                if path.endswith('.html'):
                    return Response(content, mimetype='text/html')
                elif path.endswith('.css'):
                    return Response(content, mimetype='text/css')
                elif path.endswith('.js'):
                    return Response(content, mimetype='application/javascript')
                else:
                    return Response(content, mimetype='text/plain')
            except:
                pass
    
    # Check for index.html
    index_path = os.path.join(workspace_dir, "index.html")
    if os.path.exists(index_path):
        try:
            with open(index_path, 'r', encoding='utf-8') as f:
                return Response(f.read(), mimetype='text/html')
        except:
            pass
    
    # Get output for display
    output = ""
    if session_id in container_outputs:
        output = "".join(container_outputs[session_id][-50:])  # Last 50 lines
    
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Code Execution - {session_id}</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{ 
                font-family: 'Consolas', 'Monaco', monospace; 
                padding: 20px; 
                background: #1e1e2f; 
                color: #e0e0e0; 
                line-height: 1.6;
            }}
            .container {{ max-width: 800px; margin: 0 auto; }}
            .status {{ 
                background: #27293d; 
                padding: 15px; 
                border-radius: 8px; 
                margin: 10px 0;
                border-left: 4px solid #5a87ff;
            }}
            .output {{ 
                background: #161625; 
                padding: 15px; 
                border-radius: 8px; 
                margin: 10px 0;
                font-family: 'Courier New', monospace;
                white-space: pre-wrap;
                max-height: 400px;
                overflow-y: auto;
                border: 1px solid #3b3d55;
            }}
            .refresh-btn {{
                background: #5a87ff;
                color: white;
                border: none;
                padding: 10px 20px;
                border-radius: 5px;
                cursor: pointer;
                margin: 10px 0;
            }}
            .file-list {{
                background: #27293d;
                padding: 15px;
                border-radius: 8px;
                margin: 10px 0;
            }}
            .file-item {{
                background: #3b3d55;
                padding: 8px 12px;
                margin: 5px 0;
                border-radius: 4px;
                cursor: pointer;
            }}
            .file-item:hover {{
                background: #4a4c6b;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🚀 Code Execution Environment</h1>
            
            <div class="status">
                <strong>Session ID:</strong> <code>{session_id}</code><br>
                <strong>Status:</strong> Running<br>
                <strong>Workspace:</strong> /tmp/workspace_{session_id}
            </div>
            
            <button class="refresh-btn" onclick="location.reload()">🔄 Refresh</button>
            
            <div class="output" id="output">{output or 'No output yet... Execute commands in the terminal to see results here.'}</div>
        </div>
        
        <script>
            setInterval(() => {{
                fetch('/get_output/{session_id}')
                    .then(r => r.json())
                    .then(data => {{
                        if (data.output) {{
                            document.getElementById('output').textContent = data.output;
                        }}
                    }})
                    .catch(e => console.log('Fetch error:', e));
            }}, 2000);
        </script>
    </body>
    </html>
    """

@app.route("/get_output/<session_id>")
def get_output(session_id):
    if session_id not in active_terminals:
        return jsonify({"error": "Terminal not found"}), 404
    
    output = ""
    if session_id in container_outputs:
        output = "".join(container_outputs[session_id])
    
    return jsonify({
        "status": "running",
        "output": output
    })

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
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
