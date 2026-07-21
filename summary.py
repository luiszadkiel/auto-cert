import json, glob, os
from collections import Counter
from datetime import datetime

files = glob.glob('output/jks_discovery_*.json')
if not files:
    print("No se encontraron archivos JSON.")
    exit(1)
    
latest_file = max(files, key=os.path.getctime)
with open(latest_file, 'r', encoding='utf-8') as f:
    data = json.load(f)

certs = data.get('certificados', [])

print(f'Total JKS Extraídos (Certificados/Alias): {len(certs)}')

clusters = Counter(c.get('cluster') for c in certs)
print('\n[CERTIFICADOS POR CLUSTER]')
for k, v in clusters.most_common():
    print(f'  - {k}: {v} certs')

namespaces = Counter(c.get('ambiente') for c in certs)
print('\n[CERTIFICADOS POR AMBIENTE (Namespace)]')
for k, v in namespaces.most_common():
    print(f'  - {k}: {v} certs')

ahora = datetime.utcnow().isoformat()[:19]
vencidos = [c for c in certs if c.get('fecha_vencimiento_certificado') and c['fecha_vencimiento_certificado'][:19] < ahora]

print(f'\n[CERTIFICADOS VENCIDOS]: {len(vencidos)}')
for c in vencidos:
    print(f'  [!] {c.get("cluster")} | {c.get("ambiente")} | {c.get("secreto_k8s")} -> Venció el: {c.get("fecha_vencimiento_certificado")[:10]}')
