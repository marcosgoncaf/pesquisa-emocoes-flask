import os
import json
import random
import string
from datetime import datetime
from flask import Flask, render_template, request, jsonify

# --- IMPORTAÇÕES CLOUDINARY ---
import cloudinary
import cloudinary.uploader

app = Flask(__name__)
# Limite de 60MB para aceitar vídeos no upload
app.config['MAX_CONTENT_LENGTH'] = 60 * 1024 * 1024 

# =============================================================================
# --- CONFIGURAÇÃO CLOUDINARY (PREENCHA COM SEUS DADOS) ---
# =============================================================================
cloudinary.config( 
  cloud_name = "dhbiml2um", 
  api_key = "354775456684459", 
  api_secret = "r9KVE03YmyzlGRV4qOy3Iux8a-E",
  secure = True
)

_sheets_client = None

# --- CONEXÃO GOOGLE SHEETS (Para salvar dados) ---
def get_sheets_service():
    global _sheets_client
    if _sheets_client: return _sheets_client

    print("🔌 Conectando Google Sheets...")
    import gspread
    from google.oauth2 import service_account
    
    SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
    
    creds = None
    if os.path.exists("credentials.json"):
        creds = service_account.Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    else:
        creds_json = os.environ.get("GOOGLE_CREDENTIALS")
        if creds_json:
            creds = service_account.Credentials.from_service_account_info(json.loads(creds_json), scopes=SCOPES)
    
    if not creds: raise Exception("Credenciais Google não encontradas.")

    gc = gspread.authorize(creds)
    _sheets_client = gc.open("Resultados Pesquisa Emoções")
    return _sheets_client

# --- AUXILIARES ---
def calculate_implicit_score(emotions_list):
    """Calcula nota de 0 a 10 baseada nas emoções"""
    if not emotions_list: return 0
    score_map = {'happy': 10.0, 'surprise': 8.0, 'neutral': 5.0, 'sad': 3.0, 'fear': 2.0, 'angry': 1.0, 'disgust': 0.0}
    total = 0; valid = 0
    for e in emotions_list:
        if e in score_map:
            total += score_map[e]; valid += 1
    return round(total/valid, 1) if valid > 0 else 0

def decode_image_lazy(base64_string):
    """Decodifica imagem para o DeepFace"""
    import base64; import numpy as np; import cv2
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
            sh = get_sheets_service()
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
        # Garante conexão antes do processamento pesado
        sh = get_sheets_service()
        
        form = request.form
        files = request.files
        items = []
        i = 0
        
        while True:
            if f'items[{i}][name]' not in form: break
            
            input_type = form.get(f'items[{i}][inputType]') # 'upload' ou 'url'
            direct_url = form.get(f'items[{i}][directUrl]')
            file_obj = files.get(f'items[{i}][file]')
            
            final_url = ""
            ftype = "image"

            # --- LÓGICA 1: UPLOAD VIA CLOUDINARY ---
            if input_type == 'upload' and file_obj and file_obj.filename:
                print(f"⬆️ Iniciando upload Cloudinary: {file_obj.filename}")
                
                # Define se é video ou imagem para o Cloudinary
                res_type = "video" if file_obj.mimetype.startswith('video') else "image"
                
                # Upload direto
                upload_result = cloudinary.uploader.upload(
                    file_obj.stream, 
                    resource_type = res_type,
                    folder = "estudo_emocoes"
                )
                
                final_url = upload_result.get('secure_url')
                ftype = res_type
                print(f"✅ Upload concluído: {final_url}")

            # --- LÓGICA 2: LINK EXTERNO ---
            elif input_type == 'url' and direct_url:
                final_url = direct_url
                if any(x in direct_url.lower() for x in ['.mp4', '.mov', 'youtube']): 
                    ftype = 'video'
            
            if not final_url: 
                return jsonify({'status':'error', 'message':f"Item {i+1}: Selecione um arquivo ou cole um link."}), 400

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
        cfg = {"study_name": form.get('study_name'), "welcome_message": form.get('welcome_message'), "items": items, "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        
        try: ws = sh.worksheet("Estudos")
        except: ws = sh.add_worksheet("Estudos", 100, 5); ws.append_row(["ID", "Config"])
        ws.append_row([sid, json.dumps(cfg)])
        
        return jsonify({'status': 'success', 'link': f"{request.host_url.rstrip('/')}/?study_id={sid}"})

    except Exception as e:
        print(f"Erro Fatal: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/check_face', methods=['POST'])
def check_face():
    try:
        import cv2
        img = decode_image_lazy(request.json['image'])
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        # Tolerância alta (minNeighbors=3) para check-in fácil
        faces = cascade.detectMultiScale(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 1.1, 3)
        return jsonify({'face_detected': len(faces) > 0})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/analyze_emotion', methods=['POST'])
def analyze_emotion_route():
    try:
        from deepface import DeepFace
        img = decode_image_lazy(request.json['image'])
        try:
            # enforce_detection=False: Permite analisar mesmo se o rosto não estiver perfeito (Correção dos 0 frames)
            res = DeepFace.analyze(img_path=img, actions=['emotion'], enforce_detection=False, detector_backend='opencv', silent=True)
            dom = res[0]['dominant_emotion'] if isinstance(res, list) else res['dominant_emotion']
            return jsonify({'status': 'success', 'emotion': dom})
        except Exception as e:
            # Se falhar mesmo assim, loga e retorna erro tratado
            print(f"DeepFace Warning: {e}")
            return jsonify({'status': 'error', 'emotion': 'neutral'}) 
    except: return jsonify({'status': 'error', 'emotion': None})

@app.route('/save_data', methods=['POST'])
def save_data():
    try:
        sh = get_sheets_service()
        data = request.json
        pid = data.get('participant_id'); sid = data.get('study_id'); results = data.get('results', [])
        
        rows = []
        for item in results:
            emotions = item.get('emotions_list', [])
            # Remove erros da lista
            valid_emotions = [e for e in emotions if e and e != 'erro' and e != 'no_face']
            
            main_emo = max(set(valid_emotions), key=valid_emotions.count) if valid_emotions else "Inconclusivo"
            implicit_score = calculate_implicit_score(valid_emotions)
            
            # --- NOVA ORDEM DA TABELA SOLICITADA ---
            rows.append([
                pid,                                    # 1. Participante
                sid,                                    # 2. ID Estudo
                item.get('stimulus'),                   # 3. Estímulo
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"), # 4. Data
                item.get('duration_config', 0),         # 5. Tempo(s)
                item.get('fps_config', 0),              # 6. FPS
                item.get('total_frames', 0),            # 7. Total Frames
                item.get('valid_frames', 0),            # 8. Frames Válidos
                main_emo,                               # 9. Emoção Dominante
                implicit_score,                         # 10. Nota Implícita
                ", ".join(valid_emotions),              # 11. Lista Emoções
                item.get('liking'),                     # 12. Nota Explícita
                item.get('word'),                       # 13. Palavra
                item.get('explicit_emotions')           # 14. Emoções Explícitas (Texto)
            ])
            
        try: ws = sh.worksheet("Resultados")
        except: 
            ws = sh.add_worksheet("Resultados", 1000, 15)
            ws.append_row(["Participante", "ID Estudo", "Estímulo", "Data", "Tempo(s)", "FPS", "Total Frames", "Frames Válidos", "Emoção Dominante", "Scor Implícito", "Lista Emoções", "Nota Explícita", "Palavra", "Emoções Explícitas"])
            
        ws.append_rows(rows)
        return jsonify({'status': 'success'})
    except Exception as e: return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)