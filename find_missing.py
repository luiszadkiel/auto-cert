import subprocess, json

print("Buscando suscripciones...")
subs_out = subprocess.check_output(['az', 'account', 'list', '--query', '[].id', '-o', 'json'])
subs = json.loads(subs_out)

all_clusters = set()
for s in subs:
    try:
        cs_out = subprocess.check_output(['az', 'aks', 'list', '--subscription', s, '--query', '[].name', '-o', 'json'])
        cs = json.loads(cs_out)
        all_clusters.update(cs)
    except:
        pass

rep = json.loads(open('output/jks_discovery_20260627_200302.json').read())
rep_clusters = set(x.get('cluster', '') for x in rep)

missing = all_clusters - rep_clusters

print(f"Total clusters en Azure: {len(all_clusters)}")
print(f"Clusters que sí entró (en reporte): {len(rep_clusters)}")
print(f"Clusters a los que NO pudo entrar ({len(missing)}):")
for m in sorted(list(missing)):
    print(f" - {m}")
