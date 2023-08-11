import datetime, os, requests, jsonpickle
from kubernetes import client, config
from flask import Flask, render_template,  request, redirect, url_for, jsonify, render_template_string, make_response, Response, session
import threading, time
from apscheduler.schedulers.background import BackgroundScheduler
import logging
from flask_session import Session
from threading import Lock
from kubernetes.client import ApiException
from kubernetes.client import AppsV1Api
import argparse
from flask_socketio import SocketIO, emit

# logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = Flask(__name__)
socketio = SocketIO(app)


# Create the argument parser
parser = argparse.ArgumentParser()

# Add the optional argument for the kubeconfig file path
parser.add_argument("--kubeconfig", help="Path to the kubeconfig file", type=str)
# Add the threshold argument
parser.add_argument('--threshold', type=int, help='Time threshold for Pods in a bad state (in minutes).')

# Parse the arguments
args = parser.parse_args()

# Parse the command line arguments
args = parser.parse_args()


def generate_html_message():
    html_message = "Testing Deletion of pods"
    yield html_message

# Function to add print statements to a list
def add_print_statements(statement_list, *args):
    for arg in args:
        statement_list.append(arg)

# Function to print the list items with new lines
def print_list_items(statement_list):
    for statement in statement_list:
        print(statement)

delete_only_pods_global = False


@app.route('/toggle_delete_only_pods', methods=['POST'])
def toggle_delete_only_pods():
    global delete_only_pods_global
    delete_only_pods_global = not delete_only_pods_global
    return jsonify({"status": "success"})

pauseActions = False
pauseActions_lock = Lock()

# app.config['SESSION_TYPE'] = 'filesystem'
# app.config['SECRET_KEY'] = 'supersecretkey'
# Session(app)

@app.route('/get_pause_actions', methods=['GET'])
def get_pause_actions():
    with pauseActions_lock:
        return {'pauseActions': pauseActions}

@app.route('/set_pause_actions', methods=['POST'])
def set_pause_actions():
    data = request.get_json()
    global pauseActions
    with pauseActions_lock:
        pauseActions = data['pauseActions']
    return {'result': 'success'}

# Set the default value for POD_BAD_STATE_THRESHOLD
POD_BAD_STATE_THRESHOLD = 6

# Create an argument parser
parser2 = argparse.ArgumentParser(description='Set the time threshold for Pods in a bad state (in minutes).')
parser2.add_argument('--threshold', type=int, help='Time threshold for Pods in a bad state (in minutes).')

# Parse the arguments
args2 = parser2.parse_args()

# Override the default value with the value from the command line (if provided)
if args.threshold:
    POD_BAD_STATE_THRESHOLD = args.threshold
else:
    POD_BAD_STATE_THRESHOLD = 6
print("")
print("<----------------->")
print(f"Un-Healthy pod deletion threshold set to {POD_BAD_STATE_THRESHOLD}")
print("<----------------->")
print("")

def main(kubeconfig=None):

    global delete_only_pods_global
    global pauseActions
    statements = []
    try:
        if kubeconfig:
            # Use the specified kubeconfig file
            config.load_kube_config(kubeconfig)
        else:
            # Use the default kubeconfig file
            config.load_kube_config()
    except Exception as e:
        print("Local kubeconfig not loaded:", e)
        return
    
    # Create a Kubernetes API client
    api_client = client.CoreV1Api()
    apps_v1_api = client.AppsV1Api()  # Add this line to create an AppsV1Api client


    # List all the Pods in all namespaces

    try:
        # List all the Pods in all namespaces
        pods = api_client.list_pod_for_all_namespaces(watch=False)
    except Exception as e:
        print("Local kubeconfig is unhealthy, cannot connect to cluster:", e)
        return
    

    print_pod_status  = []
    # Loop through the Pods and perform actions on those in a bad state
    statements_bad = []
    for pod in pods.items:
        
        # Check if the Pod is in the Running state and has container statuses
        if pod.status.phase == "Running" and pod.status.container_statuses:
            # Initialize the start time to None
            start_time = None
            
            # Loop through the container statuses and check for bad states
            for container_status in pod.status.container_statuses:
                # print(pod.status.container_statuses)
                if container_status.state.waiting and container_status.state.waiting.reason:
                    if container_status.state.waiting.reason == "CrashLoopBackOff":
                        start_time = container_status.last_state.terminated.started_at
                        # print(f"Pod {pod.metadata.name} in namespace {pod.metadata.namespace} is in CrashLoopBackOff state")
                        
                        # add_print_statements(statements, f"Pod {pod.metadata.name} in namespace {pod.metadata.namespace} is in CrashLoopBackOff state")
                    elif container_status.state.waiting.reason == "ErrImagePull":
                        # Log the error state
                        start_time = container_status.last_state.terminated.started_at
                        # print(f"Pod {pod.metadata.name} in namespace {pod.metadata.namespace} is in ErrImagePull state")
                elif container_status.state.terminated:
                    if container_status.state.terminated.reason == "Completed":
                        # Log the completed state
                        start_time = container_status.last_state.terminated.started_at
                        # print(f"Pod {pod.metadata.name} in namespace {pod.metadata.namespace} is in Completed state")
            
            # If the start time is set, calculate the elapsed time and take action if necessary
            if start_time:
                current_time = datetime.datetime.now(datetime.timezone.utc)
                elapsed_time = current_time - start_time
                elapsed_minutes = elapsed_time.total_seconds() / 60.0
                # print(f"Pod {pod.metadata.name} in namespace {pod.metadata.namespace} is in {container_status.state.waiting.reason} state for {elapsed_minutes:.2f} minutes")
                add_print_statements(statements_bad, f"Pod {pod.metadata.name} in namespace {pod.metadata.namespace} is in unhealthy state for {elapsed_minutes:.2f} minutes")
                global pauseActions
                with pauseActions_lock:
                    should_delete = not pauseActions
                if elapsed_minutes > POD_BAD_STATE_THRESHOLD and should_delete:
                    if not delete_only_pods_global:
                        deployment_owner = None
                        if pod.metadata.owner_references is not None:

                            for owner in pod.metadata.owner_references:
                                if owner.kind == "ReplicaSet":
                                    deployment_owner = owner
                                    break
                            if deployment_owner:
                                # Find the deployment that manages this pod
                                deployments = apps_v1_api.list_namespaced_deployment(pod.metadata.namespace)
                                for deployment in deployments.items:
                                    match_labels = deployment.spec.selector.match_labels
                                    if all(item in pod.metadata.labels.items() for item in match_labels.items()):
                                        # Delete the deployment
                                        print("----------------")
                                        print(f"Deleting Unhealthy Deployment {deployment.metadata.name} in namespace {pod.metadata.namespace}")
                                        print("----------------")
                                        try:
                                            apps_v1_api.delete_namespaced_deployment(
                                                name=deployment.metadata.name,
                                                namespace=pod.metadata.namespace,
                                                body=client.V1DeleteOptions(
                                                    grace_period_seconds=0,
                                                    propagation_policy='Foreground',
                                                ),
                                            )
                                        except ApiException as e:
                                            print(f"Failed to delete Deployment {deployment.metadata.name}: {e}")
                                        break
                    # Log the bad state and delete the Pod
                    print("########################")
                    print("")
                    print(f"Pod {pod.metadata.name} in namespace {pod.metadata.namespace} is in CrashLoopBackOff state for more than {elapsed_minutes:.2f} minutes")
                    print(f"Deleting Pod {pod.metadata.name} in namespace {pod.metadata.namespace}")
                    print("########################")
                    print("")
                    api_client.delete_namespaced_pod(pod.metadata.name, pod.metadata.namespace, body=client.V1DeleteOptions(grace_period_seconds=0, propagation_policy='Background'))
                    print("")

        # Check if the Pod is in the Pending state and has container statuses
        elif pod.status.phase == "Pending" and pod.status.container_statuses:
            for container_status in pod.status.container_statuses:
                if container_status.state.waiting and container_status.state.waiting.reason == "ImagePullBackOff":
                    # Get the start time of the Pod and calculate the elapsed time
                    start_time = pod.status.start_time
                    current_time = datetime.datetime.now(datetime.timezone.utc)
                    elapsed_time = current_time - start_time
                    elapsed_minutes = elapsed_time.total_seconds() / 60.0
                    add_print_statements(statements_bad, f"Pod {pod.metadata.name} in namespace {pod.metadata.namespace} is in {container_status.state.waiting.reason} state for {elapsed_minutes:.2f} minutes")
                    # global pauseActions
                    with pauseActions_lock:
                        should_delete = not pauseActions
                    if elapsed_minutes > POD_BAD_STATE_THRESHOLD and should_delete:
                        # print("333333")
                        # print(delete_only_pods)
                        if not delete_only_pods_global:
                            deployment_owner = None
                            if pod.metadata.owner_references is not None:

                                for owner in pod.metadata.owner_references:
                                    if owner.kind == "ReplicaSet":
                                        deployment_owner = owner
                                        break
                                if deployment_owner:
                                    # Find the deployment that manages this pod
                                    deployments = apps_v1_api.list_namespaced_deployment(pod.metadata.namespace)
                                    for deployment in deployments.items:
                                        match_labels = deployment.spec.selector.match_labels
                                        if all(item in pod.metadata.labels.items() for item in match_labels.items()):
                                            # Delete the deployment
                                            print("------ImagePullBackOff----------")
                                            print(f"Deleting Unhealthy Deployment  {deployment.metadata.name} in namespace {pod.metadata.namespace}")
                                            print("------ImagePullBackOff--------")
                                            try:
                                                apps_v1_api.delete_namespaced_deployment(
                                                    name=deployment.metadata.name,
                                                    namespace=pod.metadata.namespace,
                                                    body=client.V1DeleteOptions(
                                                        grace_period_seconds=0,
                                                        propagation_policy='Foreground',
                                                    ),
                                                )
                                            except ApiException as e:
                                                print(f"Failed to delete Deployment {deployment.metadata.name}: {e}")
                                            break
                        #Log the bad state and delete the Pod
                        print("")
                        print(f"Pod {pod.metadata.name} in namespace {pod.metadata.namespace} is in ImagePullBackOff state for {elapsed_minutes:.2f} minutes")
                        print(f"Deleting Pod {pod.metadata.name} in namespace {pod.metadata.namespace}")
                        print("")
                        api_client.delete_namespaced_pod(pod.metadata.name, pod.metadata.namespace, body=client.V1DeleteOptions(grace_period_seconds=0, propagation_policy='Foreground'))
        # Check if the Pod is in the Pending state and has container statuses
        elif pod.status.phase == "Pending":

            # Fetch events related to the pod
            pod_events = api_client.list_namespaced_event(pod.metadata.namespace, field_selector=f"involvedObject.name={pod.metadata.name}")
            
            # Check for "FailedScheduling" events
            failed_scheduling_events = [event for event in pod_events.items if event.reason == "FailedScheduling"]
            
            if failed_scheduling_events:
                # Log the issue (or any other action)
                add_print_statements(statements_bad, f"Pod {pod.metadata.name} in namespace {pod.metadata.namespace} has failed scheduling events.")
                
  
        if pod.metadata.deletion_timestamp != None and pod.status.phase == "Running":
            state = 'Terminating'
            print("Pod {0} in namespace {1} is in Terminating state".format(pod.metadata.name, pod.metadata.namespace))
            # print(pod.status)
            start_time = pod.status.start_time
            current_time = datetime.datetime.now(datetime.timezone.utc)
            elapsed_time = current_time - start_time
            elapsed_minutes = elapsed_time.total_seconds() / 60.0
            add_print_statements(statements_bad, f"Pod {pod.metadata.name} in namespace {pod.metadata.namespace} is in Terminating state for {elapsed_minutes:.2f} minutes")
            # global pauseActions
            with pauseActions_lock:
                should_delete = not pauseActions
            if elapsed_minutes > POD_BAD_STATE_THRESHOLD and should_delete:
                print(f"Pod {pod.metadata.name} in namespace {pod.metadata.namespace} is in Terminating state for {elapsed_minutes:.2f} minutes")
                print(f"Deleting Pod {pod.metadata.name} in namespace {pod.metadata.namespace}")
                api_client.delete_namespaced_pod(pod.metadata.name, pod.metadata.namespace, body=client.V1DeleteOptions(grace_period_seconds=0, propagation_policy='Foreground'))
        elif pod.status.phase == "Running" and pod.status.container_statuses[0].state.running is not None:
            
            print_pod_status.append(f"Pod {pod.metadata.name} in namespace {pod.metadata.namespace} is in {pod.status.phase} state ")
            add_print_statements(statements, "Pod {0} in namespace {1} is in {2} state".format(pod.metadata.name, pod.metadata.namespace, pod.status.phase))
            # print("########################")
            # print_list_items(statements)
            # print("########################")
    print("########################")
    formatted_output = f"Server received: ########################".replace('\n', '<br>')
    socketio.emit('output', formatted_output, namespace="/")

    formatted_output = "Server received:<br>" + "<br>".join(statements)
    socketio.emit('output', formatted_output, namespace="/")

    # formatted_output = f"Server received: {statements}"
    # socketio.emit('output', formatted_output, namespace="/")

    print_list_items(statements)
    print("########################")
    print("")
    formatted_output = f"Server received: ########################".replace('\n', '<br>')
    socketio.emit('output', formatted_output, namespace="/")
    formatted_output = f"Server received:   ".replace('\n', '<br>')
    socketio.emit('output', formatted_output, namespace="/")
    formatted_output = f"Server received: {statements_bad}".replace('\n', '<br>')
    socketio.emit('output', formatted_output, namespace="/")
    print_list_items(statements_bad)
    print("")
    formatted_output = f"Server received:" "  ".replace('\n', '<br>')
    socketio.emit('output', formatted_output, namespace="/")


# formatted_output = f"Server received: {statements}"
# socketio.emit('output', formatted_output, namespace="/")

# print_list_items(statements)
# Call the main function with the kubeconfig file path, if provided
if args.kubeconfig:
    main(args.kubeconfig)
else:
    main()

def get_pod_status(kubeconfig=None):
    statements = []
    if kubeconfig:
        # Use the specified kubeconfig file
        config.load_kube_config(kubeconfig)
    else:
        # Use the default kubeconfig file
        config.load_kube_config()

    # Create a Kubernetes API client
    api_client = client.CoreV1Api()

    # List all the Pods in all namespaces
    pods = api_client.list_pod_for_all_namespaces(watch=False)
    
    pod_status_list = []
    pods2 = api_client.list_namespaced_pod("default")
    print(pods2)
    # Loop through the Pods and perform actions on those in a bad state
    for pod in pods.items:

        if pod.status.phase == "Running" and pod.status.container_statuses:
            # Initialize the start time to None
            start_time = None
            
            # Loop through the container statuses and check for bad states
            for container_status in pod.status.container_statuses:
                # print(pod.status.container_statuses)
                if container_status.state.waiting and container_status.state.waiting.reason:
                    if container_status.state.waiting.reason == "CrashLoopBackOff":
                        pod_status = {
                            'namespace': pod.metadata.namespace,
                            'name': pod.metadata.name,
                            'status': container_status.state.waiting.reason,
                            # 'state': pod.status.container_statuses.state.waiting.reason,
                            'elapsed_time': None
                        }
                        # add_print_statements(statements, f"Pod {pod.metadata.name} in namespace {pod.metadata.namespace} is in CrashLoopBackOff state")
                    elif container_status.state.waiting.reason == "ErrImagePull":
                        # Log the error state
                        pod_status = {
                            'namespace': pod.metadata.namespace,
                            'name': pod.metadata.name,
                            'status': container_status.state.waiting.reason,
                            # 'state': pod.status.container_statuses.state.waiting.reason,
                            'elapsed_time': None
                        }
                elif container_status.state.terminated:
                    if container_status.state.terminated.reason == "Completed":
                        # Log the completed state
                        pod_status = {
                            'namespace': pod.metadata.namespace,
                            'name': pod.metadata.name,
                            'status': container_status.state.terminated.reason,
                            # 'state': pod.status.container_statuses.state.waiting.reason,
                            'elapsed_time': None
                        }

        elif pod.status.phase == "Pending" and pod.status.container_statuses:
            for container_status in pod.status.container_statuses:
                if container_status.state.waiting and container_status.state.waiting.reason == "ImagePullBackOff":
                    # Get the start time of the Pod and calculate the elapsed time
                        pod_status = {
                            'namespace': pod.metadata.namespace,
                            'name': pod.metadata.name,
                            'status': container_status.state.waiting.reason,
                            # 'state': pod.status.container_statuses.state.waiting.reason,
                            'elapsed_time': None
                        }

        if pod.metadata.deletion_timestamp != None and pod.status.phase == "Running":
                pod_status = {
                    'namespace': pod.metadata.namespace,
                    'name': pod.metadata.name,
                    'status': "Terminating",
                    # 'state': pod.status.container_statuses.state.waiting.reason,
                    'elapsed_time': None
                }
        elif pod.status.phase == "Running" and pod.status.container_statuses[0].state.running is not None:
            pod_status = {
                'namespace': pod.metadata.namespace,
                'name': pod.metadata.name,
                'status': pod.status.phase,
                'elapsed_time': None
            }
        pod_status_list.append(pod_status)
        # print(pod_status)
        #print(f"Pod {pod.metadata.name} in namespace {pod.metadata.namespace} is in {pod.status.phase} state ")
        #print(pod_status_list)
        add_print_statements(statements, f"Pod {pod.metadata.name} in namespace {pod.metadata.namespace} is in {pod.status.phase} state ")
    formatted_output = f"Server received: {pod_status_list}".replace('\n', '<br>')
    socketio.emit('output', formatted_output, namespace="/")
    return pod_status_list
    
 # Schedule main function to run every 10 seconds
scheduler = BackgroundScheduler()
scheduler.add_job(main, 'interval', seconds=10)
scheduler.start()


UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'yaml', 'yml'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024  # 2 MB

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS



@app.route('/upload_kubeconfig', methods=['POST'])
def upload_kubeconfig():
    try:
        uploaded_files = [file for file in request.files.values()]
        if not uploaded_files:
            return redirect(request.url)
        pod_status_dict = {}
        for file in uploaded_files:
            
            if file and allowed_file(file.filename):
                filename = f"{file.filename}"
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(file_path)
                # print("ffffff")
                # print(file_path)
                print("fffffffffffffff")
                pod_status_list = get_pod_status(kubeconfig=file_path)  # Pass the correct kubeconfig file path
                # print(pod_status_list)
                print("bbbbbbbbbbbbbbbbbb")
                pod_status_dict[filename] = pod_status_list
                formatted_output = f"Server received: {pod_status_list}".replace('\n', '<br>')
                socketio.emit('output', formatted_output, namespace="/")# Store the results in the dictionary
                # print(pod_status_dict)


        return render_template('home.html', pods_dict=pod_status_dict)
    except Exception as e:
            logger.exception("Error in upload_kubeconfig route:")
            return render_template('error.html', error_message="The server encountered an internal error and was unable to complete your request. Either the server is overloaded or there is an error in the application."), 500


@app.route('/delete_pod', methods=['POST'])
def delete_pod():
    namespace = request.form['namespace']
    pod_name = request.form['pod_name']
    # grace_period_seconds = int(request.form['grace_period_seconds'])
    # print("rfer4")
    config.load_kube_config()
    # Create a Kubernetes API client
    api_client = client.CoreV1Api()

    # Delete the pod
    # delete_pod = api_client.delete_namespaced_pod(pod_name, namespace, body=client.V1DeleteOptions(propagation_policy='Foreground'))
    delete_pod = api_client.delete_namespaced_pod(pod_name, namespace)

    print("Deleted Pod" + pod_name)
    return "Deleted Pod" + pod_name

@app.route('/delete_pod_force', methods=['POST'])
def delete_pod_force():
    namespace = request.form['namespace']
    pod_name = request.form['pod_name']
    # grace_period_seconds = int(request.form['grace_period_seconds'])
    config.load_kube_config()
    api_client = client.CoreV1Api()
    # Delete the pod
    delete_pod = api_client.delete_namespaced_pod(pod_name, namespace, body=client.V1DeleteOptions(grace_period_seconds=0, propagation_policy='Background'))
    print("Force Deleted Pod " + pod_name)
    return "Force Deleted Pod " + pod_name
 
@app.route('/', methods=['GET'])
def index():
    print("4")
    delete_only_pods = False
    print(delete_only_pods)
    return render_template('home.html', pods_dict={}, delete_only_pods=delete_only_pods)

if __name__ == '__main__':
    # app.run()
    socketio.run(app, debug=True)
        # time.sleep(10) 

# pods_dict = {}
# for kubeconfig in kubeconfig_files:
#     pods = get_pods(kubeconfig)
#     pods_dict[kubeconfig] = pods