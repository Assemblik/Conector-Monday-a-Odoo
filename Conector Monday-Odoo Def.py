import xmlrpc.client
import requests
import json
from flask import Flask, request, jsonify
from datetime import datetime
import os

app = Flask(__name__)

# ==========================================
#        CONFIGURACIÓN DE CREDENCIALES
# ==========================================
ODOO_URL = os.getenv('ODOO_URL', 'https://assemblik.odoo.com')
ODOO_DB = os.getenv('ODOO_DB', 'assemblik')
ODOO_USER = os.getenv('ODOO_USER', 'danbrito.mx@gmail.com')
ODOO_API_KEY = os.getenv('ODOO_API_KEY', '99a8a2a919186d115acb4cbda5db5b9f6932ed18')

MONDAY_API_KEY = os.getenv('MONDAY_API_KEY', 'eyJhbGciOiJIUzI1NiJ9.eyJ0aWQiOjE3OTE5NTA3OCwiYWFpIjoxMSwidWlkIjoyNzY0MDgxOCwiaWFkIjoiMjAyMi0wOS0wNVQxNTo1MjoxOS4wMDBaIiwicGVyIjoibWU6d3JpdGUiLCJhY3RpZCI6MTEwODIyMzksInJnbiI6InVzZTEifQ.p2h7mcyZxo6SQWxGx_UUotKx7QvHClt2V1l7mwmfkwU') 
MONDAY_API_URL = "https://api.monday.com/v2"

def obtener_detalles_monday(item_id):
    headers = {
        "Authorization": MONDAY_API_KEY,
        "Content-Type": "application/json",
        "API-Version": "2023-10"
    }
    query = """
    query ($id: [ID!]!) {
      items (ids: $id) {
        name
        column_values {
          id
          text
        }
      }
    }
    """
    variables = {'id': [str(item_id)]}
    try:
        response = requests.post(MONDAY_API_URL, json={'query': query, 'variables': variables}, headers=headers)
        res_json = response.json()
        return res_json['data']['items'][0] if res_json.get('data') else None
    except Exception as e:
        print(f"❌ Error API Monday: {e}")
        return None

def crear_venta_en_odoo(detalles_monday, item_id):
    try:
        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})
        models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')

        cols = {cv['id']: cv['text'] for cv in detalles_monday['column_values']}
        nombre_item_monday = detalles_monday['name'].strip() if detalles_monday['name'] else ""
        nombre_cliente     = cols.get('cliente', '').strip()
        num_cotizacion     = cols.get('texto5', '').strip()
        id_monday_col      = cols.get('id__de_elemento8', str(item_id)).strip()
        vendedor_final     = cols.get('personas', '').strip()

        meses = {1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 5: "Mayo", 6: "Junio", 
                 7: "Julio", 8: "Agosto", 9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"}
        mes_actual = meses[datetime.now().month]

        # LÓGICA DE CLIENTE ORIGINAL
        partner_id = 2 
        if nombre_cliente:
            p_ids = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'res.partner', 'search', [[['name', '=', nombre_cliente]]])
            if p_ids:
                partner_id = p_ids[0]
            else:
                # Si no existe, lo creamos (Para evitar el OdooBot que mencionabas)
                partner_id = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'res.partner', 'create', [{'name': nombre_cliente}])

        # LÓGICA DE PROYECTO ORIGINAL
        proyecto_id = False
        if nombre_item_monday:
            proj_ids = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'project.project', 'search', [[['name', '=', nombre_item_monday]]])
            if proj_ids:
                proyecto_id = proj_ids[0]
            else:
                proyecto_id = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'project.project', 'create', [{'name': nombre_item_monday}])

        venta_vals = {
            'partner_id': partner_id,
            'x_studio_vendedor': vendedor_final,
            'x_studio_referencia_monday': id_monday_col,
            'x_studio_many2one_field_dovxQ': proyecto_id,
            'x_studio_cotizacin': num_cotizacion,
            'x_studio_material_1': 'Incluido',
            'origin': f'Monday ID: {item_id}',
            'x_studio_mes_de_venta': mes_actual,
            'x_studio_modalidad_de_entrega': 'Estándar', 
            'x_studio_procesos_externos': 'N/A',
            'x_studio_tipo_1': 'Maquila',
            'x_studio_ubicacin': 'Querétaro', 
            'x_studio_metodo_de_pago_1': 'Transferencia',
            'x_studio_facturacin': 'Factura'
        }

        # --- EL CAMBIO CRÍTICO ESTÁ AQUÍ ---
        # Añadimos un contexto para desactivar automatizaciones que causan el error de 'object not bound'
        # Esto le dice a Odoo: "Crea el registro pero no ejecutes reglas automáticas de servidor"
        contexto_seguro = {
            'base_automation_pause': True, 
            'skip_workflow': True,
            'tracking_disable': True
        }

        try:
            venta_id = models.execute_kw(
                ODOO_DB, uid, ODOO_API_KEY, 
                'sale.order', 'create', 
                [venta_vals], 
                {'context': contexto_seguro}
            )
            print(f"✅ ÉXITO: Presupuesto {venta_id} creado.")
            return venta_id
        except Exception as e_inner:
            print(f"⚠️ Reintentando sin vendedor: {e_inner}")
            if 'x_studio_vendedor' in venta_vals:
                del venta_vals['x_studio_vendedor']
                venta_id = models.execute_kw(
                    ODOO_DB, uid, ODOO_API_KEY, 
                    'sale.order', 'create', 
                    [venta_vals], 
                    {'context': contexto_seguro}
                )
                return venta_id
            raise e_inner

    except Exception as e:
        print(f"❌ Error crítico en Odoo: {e}")
        return None

@app.route('/webhook/monday', methods=['POST'])
def monday_webhook():
    data = request.json
    if not data: return jsonify({"status": "error"}), 400
    if 'challenge' in data: return jsonify({'challenge': data['challenge']})
    event = data.get('event', {})
    item_id = event.get('pulseId')
    if item_id:
        detalles = obtener_detalles_monday(item_id)
        if detalles:
            crear_venta_en_odoo(detalles, item_id)
    return jsonify({"status": "processed"}), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)