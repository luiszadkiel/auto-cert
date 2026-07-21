import json
import subprocess

def run_cmd(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True, shell=True)
    if result.returncode != 0:
        raise Exception(f"Command failed: {result.stderr}")
    return result.stdout

def main():
    print("Fetching current task definition...")
    output = run_cmd("aws ecs describe-task-definition --task-definition user-service-prod-task:13 --region us-east-2")
    data = json.loads(output)
    
    task_def = data['taskDefinition']
    
    # Remove fields not allowed in register-task-definition
    keys_to_remove = ['taskDefinitionArn', 'revision', 'status', 'requiresAttributes', 'compatibilities', 'registeredAt', 'registeredBy']
    for key in keys_to_remove:
        task_def.pop(key, None)
        
    # Update image string
    task_def['containerDefinitions'][0]['image'] = '293926505005.dkr.ecr.us-east-2.amazonaws.com/user_service_atlas:latest'
    
    with open('new_task_def.json', 'w') as f:
        json.dump(task_def, f, indent=2)
        
    print("Registering new task definition...")
    register_output = run_cmd("aws ecs register-task-definition --cli-input-json file://new_task_def.json --region us-east-2")
    reg_data = json.loads(register_output)
    new_revision = reg_data['taskDefinition']['revision']
    print(f"Successfully registered revision {new_revision}.")
    
    print("Updating service...")
    update_output = run_cmd(f"aws ecs update-service --cluster Atlas-Prod --service user-service-prod --task-definition user-service-prod-task:{new_revision} --force-new-deployment --region us-east-2")
    print("Service updated and new deployment forced!")

if __name__ == '__main__':
    main()
