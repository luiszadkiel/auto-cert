import subprocess
import json
import sys

clusters_to_check = [
    "BHD-AZ-AKS-EASTUS2-API-NOPROD-001",
    "BHD-AZ-AKS-EASTUS2-POC-NOPROD-001",
    "BHD-AZ-AKS-EASTUS2-T24R21-SQA-001",
    "BHDIB-AZ-AKS-EASTUS2-IB-NOPROD-001",
    "BHDIB-AZ-AKS-EASTUS2-IB-NOPROD-002",
    "BHDL-AZ-AKS-EASTUS2-DEVOPS-001",
    "BHDL-AZ-EASTUS2-AKS-NOPROD-FRONTEND-001",
    "bhdl-az-aks-eastus2-t24r21-noprod-001"
]

def run_cmd(cmd):
    try:
        return subprocess.check_output(cmd, shell=True, text=True)
    except subprocess.CalledProcessError as e:
        return None

print("Fetching subscriptions...")
subs_out = run_cmd("az.cmd account list --query \"[].id\" -o json")
if not subs_out:
    print("Error fetching subs")
    sys.exit(1)
subs = json.loads(subs_out)

cluster_info = {}
print("Finding clusters...")
for s in subs:
    cs_out = run_cmd(f"az.cmd aks list --subscription {s} --query \"[].{{name:name, rg:resourceGroup, sub:id}}\" -o json")
    if cs_out:
        cs = json.loads(cs_out)
        for c in cs:
            # We want just the subscription ID, not the full resource ID for 'sub'
            # The 'sub' field returned by az aks list is the full ARM ID. We can use 's'.
            if c['name'] in clusters_to_check:
                cluster_info[c['name']] = {'rg': c['rg'], 'sub': s}

for c_name in clusters_to_check:
    print(f"\n=== {c_name} ===")
    info = cluster_info.get(c_name)
    if not info:
        print("  Not found in any subscription.")
        continue
    
    run_cmd(f"az.cmd account set --subscription {info['sub']}")
    
    aks_show_out = run_cmd(f"az.cmd aks show --resource-group {info['rg']} --name {c_name} --query \"{{id:id, azureRbac:aadProfile.enableAzureRbac}}\" -o json")
    if not aks_show_out:
        print("  Failed to run az aks show")
        continue
    aks_show = json.loads(aks_show_out)
    
    is_azure_rbac = aks_show.get('azureRbac')
    print(f"  Azure RBAC for Kubernetes habilitado: {is_azure_rbac}")
    
    print("  Role assignments (incluyendo grupos):")
    roles_out = run_cmd(f"az.cmd role assignment list --scope {aks_show['id']} --include-groups --query \"[].{{role:roleDefinitionName, principal:principalName, tipo:principalType}}\" -o table")
    if roles_out:
        print(roles_out.strip())
    else:
        print("  None or failed to retrieve")
