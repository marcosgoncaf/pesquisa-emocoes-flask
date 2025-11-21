import os
import json
import random
import string
import re
from datetime import datetime
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 30 * 1024 * 1024 

# SEU ID DA PASTA DO DRIVE
FOLDER_ID = '1DW-GHQLfcW6za8_fGF55urbDFWugrjdX'

_drive_service = None
_sheets_client = None

# --- CONEXÃO GOOGLE (LAZY) ---
def get_google_services():
    global _drive_service, _sheets_client
    if _drive_service and _sheets_client: return _drive_service, _sheets_client

    print("🔌 Conectando Google...")
    import gspread
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
    
    creds = None
    # Tenta local ou variáveis de ambiente
    if os.path.exists("credentials.json"):
        creds = service_account.Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    else:
        creds_json = os.environ.get("GOOGLE_CREDENTIALS")
        if creds_json:
            creds = service_account.Credentials.from_service_account_info(json.loads(creds_json), scopes=SCOPES)
    
    if not creds: raise Exception("Credenciais não encontradas.")

    _drive_service = build('drive', 'v3', credentials=creds)
    gc = gspread.authorize(creds)
    _sheets_client = gc.open("Resultados Pesquisa Emoções") # Verifique se o nome da planilha está exato
    return _drive_service, _sheets_client

# --- AUXILIARES ---
def convert_drive_link(url):
    """Converte links de compartilhamento do Drive para links de visualização direta"""
    # Tenta achar o ID
    file_id = None
    patterns = [r'/file/d/([a-zA-Z0-9_-]+)', r'id=([a-zA-Z0-9_-]+)', r'/open\?id=([a-zA-Z0-9_-]+)']
    for p in patterns:
        match = re.search(p, url)
        if match:
            file_id = match.group(1)
            break
            
    if file_id:
        # Link de exportação direta (funciona melhor para tags de imagem/video)
        return f"https://drive.google.com/uc?export=view&id={file_id}"
    return url

def decode_image_lazy(base64_string):
    import base64
    import numpy as np
    import cv2
    if ',' in base64_string: base64_string = base64_string.split(',')[1]
    nparr = np.frombuffer(base64.b64decode(base64_string), np.uint8)
    return cv2.imdecode(nparr, cv2.IMREAD_COLOR)

# --- ROTAS ---
@app.route('/')
def home():
    study_id = request.args.get('study_id')
    study_config = None
    if study_id:
        try:
            _, sh = get_google_services()
            ws = sh.worksheet("Estudos")
            cell = ws.find(study_id, in_column=1)
            if cell: study_config = json.loads(ws.cell(cell.row, 2).value)
        except Exception as e: print(f"Erro leitura estudo: {e}")
    return render_template('index.html', study_id=study_id, study_config=study_config)

@app.route('/admin')
def admin_panel(): return render_template('admin.html')

@app.route('/create_study', methods=['POST'])
def create_study():
    try:
        _, sh = get_google_services()
        form = request.form
        items = []
        i = 0
        while True:
            if f'items[{i}][name]' not in form: break
            
            direct_url = form.get(f'items[{i}][directUrl]')
            input_type = form.get(f'items[{i}][inputType]')
            
            if input_type == 'upload':
                return jsonify({'status': 'error', 'message': "Upload direto desativado pelo Google. Use Link do Drive."}), 400
            
            if not direct_url:
                return jsonify({'status': 'error', 'message': f"Item {i+1}: Link vazio."}), 400

            final_url = convert_drive_link(direct_url)
            
            # Detecção simples de vídeo
            ftype = 'image'
            if 'drive.google.com' in final_url or any(x in direct_url.lower() for x in ['.mp4', '.mov', '.avi', 'youtube']):
                ftype = 'video'

            items.append({
                "name": form.get(f'items[{i}][name]'),
                "file_data": final_url,
                "file_type": ftype,
                "caption": form.get(f'items[{i}][caption]'),
                "duration": int(form.get(f'items[{i}][duration]')),
                "fps": int(form.get(f'items[{i}][fps]')),
                "questions": {
                    "liking": form.get(f'items[{i}][q_liking]') == 'true',
                    "emotions": form.get(f'items[{i}][q_emotions]') == 'true',
                    "word": form.get(f'items[{i}][q_word]') == 'true'
                }
            })
            i += 1

        sid = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        cfg = {
            "study_name": form.get('study_name'),
            "welcome_message": form.get('welcome_message'),
            "items": items,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        
        try: ws = sh.worksheet("Estudos")
        except: ws = sh.add_worksheet("Estudos", 100, 5); ws.append_row(["ID", "Config"])
        ws.append_row([sid, json.dumps(cfg)])
        
        return jsonify({'status': 'success', 'link': f"{request.host_url.rstrip('/')}/?study_id={sid}"})
    except Exception as e: return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/check_face', methods=['POST'])
def check_face():
    try:
        import cv2
        data = request.json
        img = decode_image_lazy(data['image'])
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        # Reduzimos minNeighbors para 3 para ser mais tolerante no check-in
        faces = cascade.detectMultiScale(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 1.1, 3)
        return jsonify({'face_detected': len(faces) > 0})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/analyze_emotion', methods=['POST'])
def analyze_emotion_route():
    try:
        from deepface import DeepFace
        data = request.json
        img = decode_image_lazy(data['image'])
        
        # CONFIGURAÇÃO DE QUALIDADE
        # enforce_detection=True -> Garante que só analisa se tiver rosto real.
        # detector_backend='opencv' -> Rápido e leve.
        try:
            res = DeepFace.analyze(
                img_path=img, 
                actions=['emotion'], 
                enforce_detection=True, # AQUI ESTÁ O FILTRO DE QUALIDADE
                detector_backend='opencv',
                silent=True
            )
            dom = res[0]['dominant_emotion'] if isinstance(res, list) else res['dominant_emotion']
            return jsonify({'status': 'success', 'emotion': dom})
            
        except ValueError:
            # DeepFace lança ValueError se não achar rosto com enforce_detection=True
            return jsonify({'status': 'no_face', 'emotion': None})
            
    except Exception as e: 
        # Erro genérico do servidor ou biblioteca
        print(f"Erro DeepFace: {e}")
        return jsonify({'status': 'error', 'emotion': None})

@app.route('/save_data', methods=['POST'])
def save_data():
    try:
        _, sh = get_google_services()
        data = request.json
        pid = data.get('participant_id'); sid = data.get('study_id'); results = data.get('results', [])
        
        rows = []
        for item in results:
            emotions = item.get('emotions_list', [])
            # Remove nulos e erros da lista para calcular a moda
            valid_emotions = [e for e in emotions if e and e != 'erro' and e != 'no_face']
            
            main_emo = "Inconclusivo"
            if valid_emotions:
                main_emo = max(set(valid_emotions), key=valid_emotions.count)
            
            # Metricas de Qualidade
            total_frames = item.get('total_frames', 0)
            valid_frames = item.get('valid_frames', 0)
            fps_cfg = item.get('fps_config', 0)
            duration_cfg = item.get('duration_config', 0)
            
            rows.append([
                pid, sid, item.get('stimulus'), 
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 
                main_emo, 
                item.get('liking'), 
                ", ".join(valid_emotions), # Salva lista limpa
                item.get('word'),
                # NOVAS COLUNAS DE RELATÓRIO
                duration_cfg,
                fps_cfg,
                total_frames,
                valid_frames
            ])
            
        try: ws = sh.worksheet("Resultados")
        except: 
            ws = sh.add_worksheet("Resultados", 1000, 15)
            # Cria cabeçalho atualizado se for nova aba
            ws.append_row(["Participante", "ID Estudo", "Estímulo", "Data", "Emoção Dominante", "Nota", "Emoções Detalhadas", "Palavra", "Tempo(s)", "FPS", "Total Frames", "Frames Válidos"])
            
        ws.append_rows(rows)
        return jsonify({'status': 'success'})
    except Exception as e: return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)