import os
import json
import random
import string
import time
from datetime import datetime
from flask import Flask, render_template, request, jsonify

# --- CONFIGURAÇÃO INICIAL LEVE ---
app = Flask(__name__)
# Limite de 30MB para uploads
app.config['MAX_CONTENT_LENGTH'] = 30 * 1024 * 1024 

# ID DA PASTA NO GOOGLE DRIVE
FOLDER_ID = '1DW-GHQLfcW6za8_fGF55urbDFWugrjdX'

# Variáveis globais para cache de conexão (para não reconectar toda hora)
_drive_service = None
_sheets_client = None

# =============================================================================
# --- GERENCIADOR DE CONEXÕES (CARREGAMENTO TARDIO) ---
# =============================================================================
def get_google_services():
    """
    Conecta ao Google apenas quando necessário.
    Isso evita que o app tente conectar durante o boot (o que causaria erro 502).
    """
    global _drive_service, _sheets_client
    
    # Se já estiver conectado, retorna as conexões existentes
    if _drive_service and _sheets_client:
        return _drive_service, _sheets_client

    print("🔌 Iniciando conexão com Google Services...")
    
    # Importações movidas para cá para não pesar na inicialização do app
    import gspread
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
    
    creds = None
    # Tenta ler do arquivo local (PC)
    if os.path.exists("credentials.json"):
        creds = service_account.Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    else:
        # Tenta ler da variável de ambiente (Render)
        creds_json = os.environ.get("GOOGLE_CREDENTIALS")
        if creds_json:
            creds_dict = json.loads(creds_json)
            creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    
    if not creds:
        raise Exception("Credenciais do Google não encontradas (credentials.json ou Env Var).")

    _drive_service = build('drive', 'v3', credentials=creds)
    gc = gspread.authorize(creds)
    _sheets_client = gc.open("Resultados Pesquisa Emoções")
    
    print("✅ Conexão Google OK!")
    return _drive_service, _sheets_client

# =============================================================================
# --- FUNÇÕES AUXILIARES ---
# =============================================================================
def upload_file_to_drive(file_storage, filename):
    """Faz o upload para o Drive e retorna o Link"""
    from googleapiclient.http import MediaIoBaseUpload # Importação local
    
    drive, _ = get_google_services()
    
    file_metadata = {
        'name': f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{filename}",
        'parents': [FOLDER_ID]
    }
    
    media = MediaIoBaseUpload(file_storage, mimetype=file_storage.mimetype, resumable=True)
    
    file = drive.files().create(body=file_metadata, media_body=media, fields='id').execute()
    file_id = file.get('id')
    
    # Permissão Pública
    drive.permissions().create(fileId=file_id, body={'type': 'anyone', 'role': 'reader'}, fields='id').execute()
    
    return f"https://drive.google.com/uc?export=view&id={file_id}"

def process_base64_image(base64_string):
    """Decodifica imagem Base64 para formato OpenCV"""
    import base64
    import numpy as np
    import cv2
    
    if ',' in base64_string:
        base64_string = base64_string.split(',')[1]
    
    nparr = np.frombuffer(base64.b64decode(base64_string), np.uint8)
    return cv2.imdecode(nparr, cv2.IMREAD_COLOR)

# =============================================================================
# --- ROTAS ---
# =============================================================================

@app.route('/')
def home():
    """Rota Principal (Participante)"""
    study_id = request.args.get('study_id')
    study_config = None
    
    if study_id:
        try:
            _, sh = get_google_services()
            ws = sh.worksheet("Estudos")
            cell = ws.find(study_id, in_column=1)
            if cell:
                study_config = json.loads(ws.cell(cell.row, 2).value)
        except Exception as e:
            print(f"Erro ao buscar estudo: {e}")
            
    return render_template('index.html', study_id=study_id, study_config=study_config)

@app.route('/admin')
def admin_panel():
    """Rota Admin"""
    return render_template('admin.html')

@app.route('/create_study', methods=['POST'])
def create_study():
    """API para criar estudo (Com Upload Híbrido)"""
    try:
        form_data = request.form
        files = request.files
        
        # Verifica conexão com banco antes de processar
        _, sh = get_google_services()
        
        study_name = form_data.get('study_name')
        items = []
        index = 0
        
        while True:
            # Se não achar o nome do item X, acabou a lista
            if f'items[{index}][name]' not in form_data:
                break
            
            input_type = form_data.get(f'items[{index}][inputType]')
            direct_url = form_data.get(f'items[{index}][directUrl]')
            file_obj = files.get(f'items[{index}][file]')
            
            final_url = ""
            file_type = "image"

            # Lógica de Decisão: URL ou Arquivo?
            if input_type == 'url' and direct_url:
                final_url = direct_url
                if any(x in direct_url.lower() for x in ['.mp4', 'youtube', 'vimeo']):
                    file_type = 'video'
            
            elif input_type == 'upload' and file_obj and file_obj.filename:
                if file_obj.mimetype.startswith('video'):
                    file_type = 'video'
                final_url = upload_file_to_drive(file_obj.stream, file_obj.filename)
            
            if not final_url:
                raise Exception(f"O item {index+1} não tem arquivo nem link válido.")

            items.append({
                "name": form_data.get(f'items[{index}][name]'),
                "file_data": final_url,
                "file_type": file_type,
                "caption": form_data.get(f'items[{index}][caption]'),
                "duration": int(form_data.get(f'items[{index}][duration]')),
                "fps": int(form_data.get(f'items[{index}][fps]')),
                "questions": {
                    "liking": form_data.get(f'items[{index}][q_liking]') == 'true',
                    "emotions": form_data.get(f'items[{index}][q_emotions]') == 'true',
                    "word": form_data.get(f'items[{index}][q_word]') == 'true'
                }
            })
            index += 1

        # Salva no Sheets
        study_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        config = {
            "study_name": study_name,
            "welcome_message": form_data.get('welcome_message'),
            "items": items,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        
        try: ws = sh.worksheet("Estudos")
        except: ws = sh.add_worksheet(title="Estudos", rows=100, cols=5); ws.append_row(["ID", "Config JSON"])
        
        ws.append_row([study_id, json.dumps(config)])
        
        link = f"{request.host_url.rstrip('/')}/?study_id={study_id}"
        return jsonify({'status': 'success', 'link': link})

    except Exception as e:
        print(f"Erro Create Study: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/check_face', methods=['POST'])
def check_face():
    """API Face Check - Carrega CV2 só agora"""
    try:
        import cv2 # Importação Tardia
        
        data = request.json
        img = process_base64_image(data['image'])
        
        cascade_path = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
        face_cascade = cv2.CascadeClassifier(cascade_path)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, 1.1, 4)
        
        return jsonify({'face_detected': len(faces) > 0})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/analyze_emotion', methods=['POST'])
def analyze_emotion_route():
    """API DeepFace - Carrega TensorFlow só agora"""
    try:
        # AQUI É O SEGREDO DO SUCESSO:
        # DeepFace demora 10-20s para carregar. Se estiver no topo do arquivo, dá 502.
        # Estando aqui, só trava a primeira requisição de análise, mas o site carrega.
        from deepface import DeepFace 
        
        data = request.json
        img = process_base64_image(data['image'])
        
        # 'opencv' backend é mais rápido que 'ssd' ou 'mtcnn'
        analysis = DeepFace.analyze(img_path=img, actions=['emotion'], enforce_detection=False, detector_backend='opencv')
        
        dominant = "não_detectado"
        if isinstance(analysis, list) and len(analysis) > 0:
            dominant = analysis[0]['dominant_emotion']
        elif isinstance(analysis, dict):
            dominant = analysis['dominant_emotion']
            
        return jsonify({'emotion': dominant})
    except Exception as e:
        print(f"Erro DeepFace: {e}")
        return jsonify({'emotion': 'erro'})

@app.route('/save_data', methods=['POST'])
def save_data():
    try:
        _, sh = get_google_services()
        data = request.json
        
        pid = data.get('participant_id')
        sid = data.get('study_id')
        results = data.get('results', [])
        
        rows = []
        for item in results:
            emotions = item.get('emotions_list', [])
            # Pega a moda (mais comum) ou a primeira
            main_emo = "N/A"
            if emotions:
                main_emo = max(set(emotions), key=emotions.count)
                
            rows.append([
                pid, sid, item.get('stimulus'),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                main_emo,
                item.get('liking'),
                ", ".join(map(str, emotions)),
                item.get('word')
            ])
            
        try: ws = sh.worksheet("Resultados")
        except: ws = sh.add_worksheet("Resultados", 1000, 10)
        
        ws.append_rows(rows)
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)