import xmlrpc.client
import requests
import json
import os
import pdfplumber
import base64
import time
import re
import random
import hashlib
from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)

# --- CONFIGURACIÓN ---
ODOO_URL = os.getenv('ODOO_URL')
ODOO_DB = os.getenv('ODOO_DB')
ODOO_USER = os.getenv('ODOO_USER')
ODOO_API_KEY = os.getenv('ODOO_API_KEY')
MONDAY_API_KEY = os.getenv('MONDAY_API_KEY')
MONDAY_API_URL = "https://api.monday.com/v2"

# SEMÁFORO GLOBAL PARA EVITAR DUPLICADOS SIMULTÁNEOS
archivos_en_proceso = {}

def limpiar_monto_proximidad(texto):
    if not texto: return 0.0
    try:
        limpio = re.sub(r'[^0-9.]', '', str(texto).replace(',', ''))
        return float(limpio) if limpio else 0.0
    except: return 0.0

def extraer_lineas_pdf(pdf_content, maquila_id):
    lineas_acumuladas = []
    MAPA_ACEROS = {
        "SS304": "SS 304", "SS316": "SS 316", "A36": "AC A36",
        "1018": "AC 1018", "1045": "AC 1045", "4140": "AC 4140",
        "GALVANIZADO": "Galvanizado", "AL6160": "AL 6160"
    }
    
    path_temp = f"temp_{int(time.time())}.pdf"
    with open(path_temp, "wb") as f: f.write(pdf_content)
    
    try:
        with pdfplumber.open(path_temp) as pdf:
            contador_pieza = 1
            for page in pdf.pages:
                words = page.extract_words()
                if not words: continue
                lines_dict = {}
                for w in words:
                    y = round(w['top'] / 3) * 3 
                    if y not in lines_dict: lines_dict[y] = []
                    lines_dict[y].append(w)
                
                for y in sorted(lines_dict.keys()):
                    line_words = sorted(lines_dict[y], key=lambda x: x['x0'])
                    text_full = " ".join([w['text'] for w in line_words])
                    if not text_full.startswith("Maq-"): continue
                    
                    try:
                        partida = line_words[0]['text']
                        
                        # Extraer Precio Unitario
                        montos = re.findall(r'\$\s?([\d,]+\.\d{2})', text_full)
                        pu_final = 0.0
                        if len(montos) >= 2: pu_final = limpiar_monto_proximidad(montos[-2])
                        elif montos: pu_final = limpiar_monto_proximidad(montos[0])

                        # Extraer Espesor y Acero
                        espesor = "N/A"
                        idx_esp = -1
                        for i, w in enumerate(line_words):
                            t = w['text']
                            if '/' in t or '"' in t or re.match(r'^[Cc]\d+$', t):
                                espesor = t; idx_esp = i; break
                        
                        metal_raw = "AC A36"
                        if idx_esp != -1 and (idx_esp + 1) < len(line_words):
                            metal_raw = line_words[idx_esp + 1]['text'].replace(" ", "").upper()

                        metal_final = "AC A36"
                        for clave_pdf, valor_odoo in MAPA_ACEROS.items():
                            if clave_pdf in metal_raw: metal_final = valor_odoo; break

                        # --- NUEVA LÓGICA DINÁMICA DE MATERIAL (Si/No) ---
                        # Buscamos si en la línea existe la palabra "Si" o "No" (ignorando mayúsculas)
                        material_valor = "Si" # Valor por defecto
                        if re.search(r'\bno\b', text_full, re.IGNORECASE):
                            material_valor = "No"
                        elif re.search(r'\bsi\b', text_full, re.IGNORECASE):
                            material_valor = "Si"

                        lineas_acumuladas.append((0, 0, {
                            'product_id': maquila_id,
                            'x_studio_descripcion': f"Pieza {contador_pieza}",
                            'x_studio_partida_1': partida,
                            'x_studio_espesor': espesor,
                            'x_studio_acero': metal_final,
                            'x_studio_material': material_valor, # <--- DINÁMICO
                            'product_uom_qty': 1.0,
                            'product_uom_id': 1,
                            'price_unit': pu_final,
                        }))
                        contador_pieza += 1
                    except: continue
        return lineas_acumuladas
    except Exception: return []
    finally:
        if os.path.exists(path_temp): os.remove(path_temp)

def procesar_flujo(item_id, pulse_name):
    time.sleep(1.5)
    try:
        headers = {"Authorization": MONDAY_API_KEY, "API-Version": "2023-10"}
        query = f'query {{ items (ids: [{item_id}]) {{ assets {{ name url public_url }} column_values {{ id text }} }} }}'
        r = requests.post(MONDAY_API_URL, json={'query': query}, headers=headers).json()
        items = r.get('data', {}).get('items', [])
        if not items: return
        item_data = items[0]
        assets = item_data.get('assets', [])
        cols_text = {c['id']: (c.get('text') or "") for c in item_data.get('column_values', [])}

        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})
        models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')

        maquila_search = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'product.product', 'search', [[['name', '=', 'Maquila']]])
        maquila_id = maquila_search[0] if maquila_search else False

        proj_ids = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'project.project', 'search', [[['name', '=', pulse_name]]])
        proyecto_id = proj_ids[0] if proj_ids else models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'project.project', 'create', [{'name': pulse_name}])

        lineas_finales = []
        for asset in reversed(assets):
            nombre = asset.get('name', '').lower()
            if nombre.endswith(".pdf"):
                u = asset['public_url'] if asset.get('public_url') else asset['url']
                pdf_data = requests.get(u).content
                lineas_extraidas = extraer_lineas_pdf(pdf_data, maquila_id)
                if lineas_extraidas:
                    lineas_finales = lineas_extraidas
                    break

        nombre_cli = cols_text.get('cliente', 'Cliente Monday').strip()
        cli_ids = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'res.partner', 'search', [[['name', '=', nombre_cli]]])
        partner_id = cli_ids[0] if cli_ids else models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'res.partner', 'create', [{'name': nombre_cli}])

        mes = {1:"Enero",2:"Febrero",3:"Marzo",4:"Abril",5:"Mayo",6:"Junio",7:"Julio",8:"Agosto",9:"Septiembre",10:"Octubre",11:"Noviembre",12:"Diciembre"}[datetime.now().month]

        venta_vals = {
            'partner_id': partner_id,
            'x_studio_many2one_field_dovxQ': proyecto_id,
            'x_studio_referencia_monday': str(item_id),
            'x_studio_vendedor': cols_text.get('personas', 'Sin Vendedor'),
            'x_studio_cotizacin': cols_text.get('texto5', ''),
            'x_studio_mes_de_venta': mes,
            'x_studio_material_1': 'Incluido',
            'x_studio_tipo_1': 'Maquila',
            'x_studio_ubicacin': 'Querétaro',
            'x_studio_facturacin': 'Factura',
            'x_studio_metodo_de_pago_1': 'Transferencia',
            'x_studio_modalidad_de_entrega': 'Estándar',
            'x_studio_procesos_externos': 'N/A'
        }

        existente = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'sale.order', 'search', [[['x_studio_referencia_monday', '=', str(item_id)]]])
        if existente:
            order_id = existente[0]
            if lineas_finales: venta_vals['order_line'] = [(5, 0, 0)] + lineas_finales
            models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'sale.order', 'write', [[order_id], venta_vals])
            print(f"🔄 Pedido {order_id} actualizado.")
        else:
            if lineas_finales: venta_vals['order_line'] = lineas_finales
            order_id = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'sale.order', 'create', [venta_vals])
            print(f"✅ Pedido creado: {order_id}")

        # --- GESTIÓN DE ADJUNTOS CON SEMÁFORO ANTI-DUPLICADOS ---
        adjuntos_odoo = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'ir.attachment', 'search_read', 
            [[['res_model', '=', 'sale.order'], ['res_id', '=', order_id]]], {'fields': ['checksum', 'name']})
        
        hashes_existentes = [a['checksum'] for a in adjuntos_odoo]
        nombres_existentes = [a['name'] for a in adjuntos_odoo]

        for f in assets:
            nombre_f = f['name']
            ahora = time.time()
            
            if nombre_f in archivos_en_proceso:
                if ahora - archivos_en_proceso[nombre_f] < 10:
                    continue

            if nombre_f in nombres_existentes:
                continue

            u_f = f['public_url'] if f.get('public_url') else f['url']
            content = requests.get(u_f).content
            f_hash = hashlib.sha1(content).hexdigest()

            if f_hash not in hashes_existentes:
                archivos_en_proceso[nombre_f] = ahora
                models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'ir.attachment', 'create', [{
                    'name': nombre_f, 'datas': base64.b64encode(content).decode('utf-8'),
                    'res_model': 'sale.order', 'res_id': order_id,
                }])
                hashes_existentes.append(f_hash)

    except Exception as e: print(f"❌ Error: {e}")

@app.route('/webhook/monday', methods=['POST'])
def webhook():
    data = request.json
    if 'challenge' in data: return jsonify({'challenge': data['challenge']})
    item_id = data.get('event', {}).get('pulseId')
    pulse_name = data.get('event', {}).get('pulseName', 'Pedido')
    if item_id: procesar_flujo(item_id, pulse_name)
    return jsonify({"status": "ok"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)