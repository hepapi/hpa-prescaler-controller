import requests
import time
import kopf
import logging
import kubernetes
from kubernetes.client.rest import ApiException
import yaml
import os
import json
import datetime
from dateutil import parser
from enum import Enum
from argocd_updater import update_argocd_app, ArgoAppUpdateStatus 
import asyncio
import random
from croniter import croniter

# disable some logs to reduce noise
logging.getLogger('aiohttp.access').setLevel(logging.WARNING)  # disables health check logs
logging.getLogger('kopf.activities').setLevel(logging.WARNING)


# ---- Configs from Env Vars
ARGOCD_ENDPOINT = os.environ.get('ARGOCD_ENDPOINT')
ARGOCD_TOKEN = os.environ.get('ARGOCD_TOKEN')
ARGOCD_SSL_VERIFY = os.environ.get('ARGOCD_SSL_VERIFY', 'false').lower() == 'true'
ARGOCD_HEALTH_CHECK_TIMEOUT = int(os.environ.get('ARGOCD_HEALTH_CHECK_TIMEOUT', '10'))

    
LOOP_INTERVAL_SECS=int(os.environ.get('LOOP_INTERVAL_SECS', '30'))
LOOP_INITAL_DELAY_SECS=int(os.environ.get('LOOP_INITAL_DELAY_SECS', '10'))
GRACE_TIME_DELTA_MINS=int(os.environ.get('GRACE_TIME_DELTA_MINS'))

RELEASE_NAMESPACE=os.environ.get('RELEASE_NAMESPACE')
DEPLOY_ENV=os.environ.get('DEPLOY_ENV', "Development")
KUBECONFIG_OR_SERVICE_ACCOUNT = os.environ.get('KUBECONFIG_OR_SERVICE_ACCOUNT', 'serviceaccount').lower()

DO_ACTUALLY_DELETE_OLD_PRESCALERS = os.environ.get('DO_ACTUALLY_DELETE_OLD_PRESCALERS', 'false').lower() == 'true'
DELETE_OLD_PRESCALERS_AFTER_DAYS = int(os.environ.get('DELETE_OLD_PRESCALERS_AFTER_DAYS', '14'))  # keep prescalers for N days
OLD_PRESCALERS_CHECK_EVERY_N_MINUTES = int(os.environ.get('OLD_PRESCALERS_CHECK_EVERY_N_MINUTES', '500'))

DO_CREATE_KUBERNETES_EVENTS = os.environ.get('DO_CREATE_KUBERNETES_EVENTS', 'false').lower() == 'true'

GRACE_WINDOW_ALGORITHM_NAME = os.environ.get('GRACE_WINDOW_ALGORITHM_NAME', 'accept_window_is_before_target_time2')

if KUBECONFIG_OR_SERVICE_ACCOUNT.lower() == 'serviceaccount':
    kubernetes.config.load_incluster_config()
elif KUBECONFIG_OR_SERVICE_ACCOUNT.lower() == 'kubeconfig':
    kubernetes.config.load_kube_config()
else:
    raise ValueError(f"Invalid env var KUBECONFIG_OR_SERVICE_ACCOUNT: {KUBECONFIG_OR_SERVICE_ACCOUNT}, only 'serviceaccount' or 'kubeconfig' are allowed")

api = kubernetes.client.CustomObjectsApi()
events_api = kubernetes.client.EventsApi()
# events_api = kubernetes.client.EventsV1Api()

class TimeStatus(Enum):
    PASSED = "passed"
    WITHIN_GRACE_WINDOW = "within_grace_window"
    NOT_STARTED = "not_started"

class OP_STATE(Enum):
    PENDING = 'PENDING'
    SUCCEEDED = 'SUCCEEDED'
    FAILED = 'FAILED'




# ---- CRON JOB FUNCTIONS ----



@kopf.timer('hpaprescalercronjobs', interval=20.0) # , initial_delay=25
def monitor_prescalers_cronjob(logger, name, namespace, status, spec, **kwargs):
    logger.info(f"[CronJob] Doing the: HpaPrescalerCronjob({name})")
    
    # check if the cron job is active
    if not spec.get('isActive', False):
        logger.info(f"[CronJob] HpaPrescalerCronjob({name}) is not active, skipping...")
        return
    
    # get the .spec.schedule and .spec.jobCount
    schedule = spec.get('schedule', False)
    job_count = spec.get('jobCount', False)
    
    if not schedule:
        logger.error(f"[CronJob] HpaPrescalerCronjob({name}) has no schedule, skipping...")
        return
    
    next_runs = []

    try:
        # parse the cron schedule and generate datetime objects
        now = datetime.datetime.now(datetime.timezone.utc).replace(second=0, microsecond=0)
        cron_schedule = croniter(schedule, now)
        for _ in range(job_count):
            next_run = cron_schedule.get_next(datetime.datetime).replace(second=0, microsecond=0)
            next_runs.append(next_run)
            
        logger.info(f"[CronJob] Generated {len(next_runs)} future run times for {name}: {', '.join([run.strftime('%Y-%m-%dT%H:%M:%SZ') for run in next_runs])}")
        
    except Exception as e:
        logger.error(f"[CronJob] Failed to parse cron schedule '{schedule}' for {name}: {str(e)}")
        return

    
    prescaler_spec = spec.get('prescalerSpec', False)
    if not prescaler_spec:
        logger.error(f"[CronJob] HpaPrescalerCronjob({name}) has no prescalerSpec, skipping...")
        return

    # Prepare prescaler spec with timeStart for each run
    prescaler_objects = []
    for run_time in next_runs:
        prescaler = {
            'apiVersion': 'hepapi.com/v1',
            'kind': 'HpaPrescaler',
            'metadata': {
                'name': f"cron-{name}--{run_time.strftime('%Y-%m-%d--%H%M')}".lower()[:63].rstrip('-'),
                'namespace': namespace, 
                'labels': {
                    'cronjob': name,
                    'created-by': 'hpa-prescaler-cronjob'
                }
            },
            'spec': prescaler_spec.copy()
        }
        # logger.info(f"[CronJob] Creating prescaler object: {json.dumps(prescaler, indent=2)}")
        prescaler['spec']['timeStart'] = run_time.strftime('%Y-%m-%dT%H:%M:%SZ')
        prescaler_objects.append(prescaler)
        
    # Initialize empty list for pending prescalers that are in the future
    pending_future_prescalers = []
    # List all prescaler objects with matching labels
    try:
        api = kubernetes.client.CustomObjectsApi()
        existing_prescalers = api.list_namespaced_custom_object(
            group="hepapi.com",
            version="v1",
            namespace=namespace,
            plural="hpaprescalers",
            label_selector=f"cronjob={name},created-by=hpa-prescaler-cronjob"
        )
        logger.debug(f"[CronJob] Found {len(existing_prescalers.get('items', []))} existing prescaler objects for {name}")

    except kubernetes.client.exceptions.ApiException as e:
        logger.error(f"[CronJob] Failed to list existing prescaler objects: {str(e)}")
        return
    

    # Filter for only PENDING prescalers
    now = datetime.datetime.now(datetime.timezone.utc)
    pending_future_prescalers = []
    for p in existing_prescalers.get('items', []):
        if (p.get('status', {}).get('state') == OP_STATE.PENDING.value and 
            parser.parse(p.get('spec', {}).get('timeStart', '')) > now):
            pending_future_prescalers.append(p)
            
    logger.info(f"[CronJob] Found {len(pending_future_prescalers)} pending future prescaler objects for {name}")
    # Get target times from pending prescalers, strip seconds
    pending_times = set()
    for p in pending_future_prescalers:
        time_start = p.get('spec', {}).get('timeStart')
        if time_start:
            dt = parser.parse(time_start).replace(second=0, microsecond=0)
            pending_times.add(dt.strftime('%Y-%m-%dT%H:%M:%SZ'))

    # Convert next_runs to set of formatted strings for comparison
    target_times = {run_time.strftime('%Y-%m-%dT%H:%M:%SZ') for run_time in next_runs}

    # Find which times need new prescaler objects
    times_needing_prescalers = target_times - pending_times

    logger.info(f"[CronJobDEBUG]  target_times {target_times} pending_times {pending_times} times_needing_prescalers {times_needing_prescalers}")
    logger.info(f"[CronJob] Need to create prescalers for {len(times_needing_prescalers)} times: {', '.join(times_needing_prescalers)}")

    times_needing_prescalers = list(times_needing_prescalers)
    # Create prescaler objects for missing times
    for prescaler in prescaler_objects:
        if prescaler['spec']['timeStart'] in times_needing_prescalers:
            try:
                # Add kind field to prescaler object
                
                api.create_namespaced_custom_object(
                    group="hepapi.com",
                    version="v1", 
                    namespace=namespace,
                    plural="hpaprescalers",
                    body=prescaler
                    # TODO add labels
                )
                logger.info(f"[CronJob] Created prescaler {prescaler['metadata']['name']}")
            except kubernetes.client.exceptions.ApiException as e:
                if e.status == 409:  # Conflict - object already exists
                    logger.warning(f"[CronJob] Prescaler {prescaler['metadata']['name']} already exists")
                else:
                    logger.error(f"[CronJob] Failed to create prescaler {prescaler['metadata']['name']}: {str(e)}")






# ---- HPA PRESCALER FUNCTIONS ----

def get_algorithm(name):
    # must return a function that takes 2 args: target_time_iso8601, grace_minutes
    # and returns a TimeStatus object
    algorithms = {
        'accept_window_is_before_target_time': accept_window_is_before_target_time,
        'accept_window_is_after_target_time': accept_window_is_after_target_time,
    }
    algo = algorithms.get(name, False)
    if not callable(algo):
        raise ValueError(f"Invalid algorithm name: {name}, must be one of: {', '.join(algorithms.keys())}")
    return algo

def accept_window_is_before_target_time(target_time_iso8601, grace_minutes=GRACE_TIME_DELTA_MINS) -> TimeStatus:
    """checks now to given target time, returns TimeStatus"""
    now = datetime.datetime.now(datetime.timezone.utc)
    grace_delta = datetime.timedelta(minutes=grace_minutes)
    target_time = parser.parse(target_time_iso8601)
    
    if now >= target_time: # already passed the target time!
        return TimeStatus.PASSED
    elif (target_time - now) <= grace_delta:
        return TimeStatus.WITHIN_GRACE_WINDOW
    else:
        return TimeStatus.NOT_STARTED
    

def accept_window_is_after_target_time(target_time_iso8601, grace_minutes=GRACE_TIME_DELTA_MINS) -> TimeStatus:
    """checks now to given target time, returns TimeStatus"""
    now = datetime.datetime.now(datetime.timezone.utc)
    grace_delta = datetime.timedelta(minutes=grace_minutes)
    target_time = parser.parse(target_time_iso8601)
    
    if now < target_time:
        return TimeStatus.NOT_STARTED
    
    # Time windows visualization:
    #                          |                          |    grace_delta
    #                          |                          |   <------------>
    # NOT_STARTED:             |    WITHIN_GRACE:         |       PASSED:
    # ----[now]------------->  |   [target]----[now]--->  |    [target]----[now]---->
    #      ▼                   |      ▼          ▼        |       ▼          ▼
    #   Current time           |  Target time  Current    |    Target    Current time
    #   before target          |  reached     time in     |    time      after grace
    #                          |              grace       |    passed     period
    
    elif now >= target_time + grace_delta:
        return TimeStatus.PASSED
    else:
        return TimeStatus.WITHIN_GRACE_WINDOW
    
    
    
    


def check_time_status_v2(target_time_iso8601, grace_minutes=GRACE_TIME_DELTA_MINS) -> TimeStatus:
    """checks now to given target time, returns TimeStatus"""
    now = datetime.datetime.now(datetime.timezone.utc)
    grace_delta = datetime.timedelta(minutes=grace_minutes)
    target_time = parser.parse(target_time_iso8601)
    
    if now < target_time:
        return TimeStatus.NOT_STARTED
    
    # Time windows visualization:
    #                          |                          |    grace_delta
    #                          |                          |   <------------>
    # NOT_STARTED:             |    WITHIN_GRACE:         |       PASSED:
    # ----[now]------------->  |   [target]----[now]--->  |    [target]----[now]---->
    #      ▼                   |      ▼          ▼        |       ▼          ▼
    #   Current time           |  Target time  Current    |    Target    Current time
    #   before target          |  reached     time in     |    time      after grace
    #                          |              grace       |    passed     period
    
    elif now >= target_time + grace_delta:
        return TimeStatus.PASSED
    else:
        return TimeStatus.WITHIN_GRACE_WINDOW


def update_status_of_prescaler_obj(name, namespace, status_body, logger):
    try:
        api.patch_namespaced_custom_object_status(
            name=name, group="hepapi.com", version='v1',
            namespace=namespace, plural="hpaprescalers",
            body={'status': status_body}
        )
        logger.debug(f"HpaPrescaler({name}) .status updated with values: {status_body}")
        return True
    except ApiException as e:
        logger.error("Exception when calling patch_namespaced_custom_object_status: %s\n" % e)
        return False


@kopf.timer('hpaprescalers', interval=OLD_PRESCALERS_CHECK_EVERY_N_MINUTES * 60.0, initial_delay=25)
def remove_old_prescalers_cronjob(logger, name, namespace, status, spec, **kwargs):
    logger.debug(f"[Old Prescaler Removal] Starting cleanup check for HpaPrescaler({name})")
    
    if not spec:
        logger.error(f"[Old Prescaler Removal] No spec provided for HpaPrescaler({name}), skipping cleanup check...")
        return

    if not status:
        logger.error(f"[Old Prescaler Removal] No status found for HpaPrescaler({name}), skipping cleanup check...")
        return
        
    try:
        current_state = status.get('state')
        
        # -- if its pending, skip
        if current_state == OP_STATE.PENDING.value:
            logger.debug(f"[Old Prescaler Removal] HpaPrescaler({name}) is in PENDING state, skipping...")
            return 

        target_time = parser.parse(spec['timeStart'])
        now = datetime.datetime.now(datetime.timezone.utc)
        age_days = (now - target_time).days
        
        # -- check if its old enough to be deleted
        if age_days >= DELETE_OLD_PRESCALERS_AFTER_DAYS:
            # delete the prescaler
            try:
                if DO_ACTUALLY_DELETE_OLD_PRESCALERS:
                    logger.info(f"[Old Prescaler Removal] Deleting HpaPrescaler({name}) older than {DELETE_OLD_PRESCALERS_AFTER_DAYS} days (age: {age_days} days) in state {current_state}")
                    api.delete_namespaced_custom_object(
                        group="hepapi.com",
                        version='v1',
                        namespace=namespace,
                        plural="hpaprescalers",
                        name=name
                    )
                else:
                    logger.info(f"[Old Prescaler Removal] Would have deleted HpaPrescaler({name}) older than {DELETE_OLD_PRESCALERS_AFTER_DAYS} days (age: {age_days} days) in state {current_state} but DO_ACTUALLY_DELETE_OLD_PRESCALERS is false")
                return 
            except ApiException as e:
                logger.error(f"[Old Prescaler Removal] Failed to delete HpaPrescaler({name}). Exception details: status={e.status}, reason={e.reason}, body={e.body}")
                return 
        else:
            logger.debug(f"[Old Prescaler Removal] HpaPrescaler({name}) is not old enough to be deleted (age: {age_days} days), skipping...")
            return 
    except KeyError:
        logger.error(f"[Old Prescaler Removal] No timeStart in spec for HpaPrescaler({name}), skipping cleanup check...")
    except ValueError as e:
        logger.error(f"[Old Prescaler Removal] Error parsing timeStart for HpaPrescaler({name}): {e}")


@kopf.daemon('hpaprescalers', initial_delay=LOOP_INITAL_DELAY_SECS)
async def monitor_hpa_prescalers(stopped, logger, name, namespace, status, spec, **kwargs):
    """Runs for each HpaPrescaler object, and waits for some time..."""
    utc_current_time_str = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    prescaler_name = f"HpaPrescaler({namespace}/{name})"
    targetProfileName = spec['targetProfileName']
    
    while not stopped:

        if not status:
            logger.warn(f"{prescaler_name} doesn't have a .status, skipping for now...")
            _non_status_delay_secs = 10
            raise kopf.TemporaryError(
                f"{prescaler_name} doesn't have a .status, skipping for {_non_status_delay_secs} seconds...", 
                delay=_non_status_delay_secs)

        timeStart = spec['timeStart']
        argocdAppName = spec['argocdAppName']
        
        if status.get('state') != OP_STATE.PENDING.value:
            # already processed
            logger.debug(f"Skipping {prescaler_name} as it's already processed, state: {status.get('state')}")
            return  # stop monitoring this obj 


        # must be pending CRD
        assert status.get('state') == OP_STATE.PENDING.value
        
        # check if the CRD still exists
        try:
            logger.debug(f"Checking if HpaPrescaler({name}) still exists in api-server...")
            api.get_namespaced_custom_object(
                group="hepapi.com",
                version='v1',
                namespace=namespace,
                plural="hpaprescalers",
                name=name
            )
        except ApiException as e:
            logger.error(f"HpaPrescaler({name}) is NOT FOUND in api-server. Stopping monitoring it...")
            return  # stop monitoring this obj 
        
        # select the algorithm to check the time status 
        time_status_check_fn = get_algorithm(GRACE_WINDOW_ALGORITHM_NAME)
        # check if the time is passed or within grace window
        time_status: TimeStatus = time_status_check_fn(timeStart, GRACE_TIME_DELTA_MINS)
        
        if time_status == TimeStatus.PASSED:
            logger.error(f"Target time for {prescaler_name} has passed.")
            create_kubernetes_event(RELEASE_NAMESPACE, 'Warning', name, 'ProcessPrescaler', 'ErrorTimeAlreadyPassed', f"Target time for {prescaler_name} has passed.", logger)
            _time_passed_status = {'state': OP_STATE.FAILED.value, "message": "Time Passed", "processedAt":utc_current_time_str} 
            _success = update_status_of_prescaler_obj(name, namespace, _time_passed_status, logger)
            if not _success:
                logger.warn(f"Can NOT patch .status of {prescaler_name} obj, its Target Time has passed.")
                raise kopf.TemporaryError(f"ERROR: can not patch .status of {prescaler_name}, retrying in 15 seconds...", delay=15)
                # return 
            
        elif time_status == TimeStatus.WITHIN_GRACE_WINDOW:
            logger.info(f"ACCEPTED {prescaler_name} as it's target time is within GraceWindow({GRACE_TIME_DELTA_MINS} mins).")

            _update_success, _app_update_status = update_hpa_of_argocd_app(name, namespace, spec, logger)

            # update .status of HpaPrescaler Object
            _updated_time_str = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            if _update_success:
                _succeeded_status = {'state': OP_STATE.SUCCEEDED.value, "message": "OK", "processedAt":_updated_time_str}
                
                if not update_status_of_prescaler_obj(name, namespace, _succeeded_status, logger):
                    logger.error(f"Failed to update .status of HpaPrescaler({name}) obj to: {json.dumps(_succeeded_status)}")
                    create_kubernetes_event(RELEASE_NAMESPACE, 'Warning', name, 'UpdatePrescalerStatus', 'FailUpdatePrescalerStatus', f"Failed to update ArgocdApp({argocdAppName}) with updated status. Failure: {_app_update_status.value}", logger)
                    logger.error(f"SUCCESS: ArgoCD app {argocdAppName} was updated successfully with profile {targetProfileName} but failed to update status of {prescaler_name}")
                    return  # stop monitoring this obj 
                    # raise kopf.TemporaryError(f"ERROR: can not patch .status of {prescaler_name}", delay=30)

                create_kubernetes_event(RELEASE_NAMESPACE, 'Normal', name, 'ProcessPrescaler', 'SuccessfullyUpdatedHPA', f"Succeeded to update the HPA of ArgocdApp({argocdAppName}) with HpaPrescaler({name})", logger)
                return  # stop monitoring this obj 
            else:
                # within grace window, but something went wrong with ArgoCD communication
                logger.error(f"Failed to update ArgocdApp({argocdAppName}). Failure: {_app_update_status}, Target Profile: {targetProfileName}")
                # _argo_issue_status = {'state': OP_STATE.FAILED.value, "message": _app_update_status.value, "processedAt":_updated_time_str}
                # update_status_of_prescaler_obj(name, namespace, _argo_issue_status, logger)
                create_kubernetes_event(RELEASE_NAMESPACE, 'Warning', name, 'UpdateArgoAppHPA', 'FailToUpdateArgoAppHPA', f"Failed to update ArgocdApp({argocdAppName}). Failure: {_app_update_status.value}", logger)
                # it's within grace window, so we can retry
                raise kopf.TemporaryError(f"Failed to communicate with and update ArgoCD app {argocdAppName}, retrying in 15 seconds...", delay=15)



        elif time_status == TimeStatus.NOT_STARTED:
            logger.debug(f"Skipping {prescaler_name} as it's in future.")

        await stopped.wait(LOOP_INTERVAL_SECS)
    return  # stop monitoring this obj 
        
        
def get_hpascaler_profiles(namespace):
    namespace = RELEASE_NAMESPACE
    hpa_profiles_list = api.list_namespaced_custom_object(
        group="hepapi.com",
        version='v1',
        namespace=namespace,
        plural="hpaprescalerprofiles"
    )
    # convert to dict of {"profile_name": {..conf..}}
    return {
        prf['metadata']['name']: prf['spec']
        for prf in hpa_profiles_list.get('items', [])
    }
    
    
def update_hpa_of_argocd_app(name, namespace, spec, logger):
    prescaler_name = f"HpaPrescaler({namespace}/{name})"
    argocdAppName = spec['argocdAppName']
    targetProfileName = spec['targetProfileName']
    
    hpa_profiles = get_hpascaler_profiles(RELEASE_NAMESPACE)
    if not hpa_profiles:
        logger.error(f"Failed to get any HpaScalerProfiles from api-server. Did you create any profiles?")
        create_kubernetes_event(RELEASE_NAMESPACE, 'Warning', name, 'GetPrescalerProfiles', 'FailToListPrescalerProfiles', f"Failed to get HpaScalerProfiles from api-server. Did you create any profiles?", logger)

        raise kopf.TemporaryError(f"Failed to get any HpaScalerProfiles from api-server. Did you create any profiles?", delay=30)

    target_profile = hpa_profiles.get(targetProfileName, False)
    if not target_profile:
        logger.error(f"Failed to find the HpaScalerProfile({targetProfileName}). Did you create a profile named '{targetProfileName}'?")
        create_kubernetes_event(RELEASE_NAMESPACE, 'Warning', name, 'GetPrescalerProfiles', 'FailToListPrescalerProfiles', f"Failed to find the HpaScalerProfile({targetProfileName}). Did you create a profile named '{targetProfileName}'?", logger)
        success = update_status_of_prescaler_obj(name, namespace, {'state': str(OP_STATE.PENDING.value), "message": f"Not Found Profile: {targetProfileName}", "processedAt":""}, logger)
        raise kopf.TemporaryError(f"Failed to find HpaScalerProfiles obj named '{targetProfileName}'", delay=60)
    
    # Actually upgrade argocd definition
    logger.info(f"Starting to update HPA of ArgoApp({argocdAppName}) Target Profile({targetProfileName})[{target_profile}]")
    success, status = update_argocd_app(argocdAppName, target_profile, logger)
    return success, status


@kopf.on.probe(id='healthcheck')
def health_check_probe(logger, **kwargs):
    # ---------- Health Check Probe ----------
    on_failure_delay = 5
    """Health check probe that tests connectivity and authentication with ArgoCD endpoint"""
    # Test endpoint (health check)
    test_url = f"{ARGOCD_ENDPOINT}/api/v1/session/userinfo"
    headers = {"Authorization": f"Bearer {ARGOCD_TOKEN}"}
    _cookies = { "argocd.token": ARGOCD_TOKEN }
    try:
        response = requests.get(test_url, headers=headers, cookies=_cookies, timeout=ARGOCD_HEALTH_CHECK_TIMEOUT, verify=ARGOCD_SSL_VERIFY)
        
        if response.status_code == 200:
            current_time = datetime.datetime.now(datetime.timezone.utc)
            formatted_time = current_time.strftime('%Y-%m-%d %H:%M:%S UTC')

            if response.json().get('loggedIn', False) == True:
                return f"Health check last passed at: {formatted_time}"  # good path
            else:
                raise kopf.TemporaryError("[HealthCheck] failed: ArgoCD endpoint reachable but authentication failed", delay=on_failure_delay)
            
        elif response.status_code in [401, 403]:
            # logger.error(f"[HealthCheck] failed: ArgoCD endpoint reachable but authentication failed (status {response.status_code})")
            raise kopf.TemporaryError("[HealthCheck] failed: ArgoCD endpoint reachable but authentication failed", delay=on_failure_delay)
        else:
            # logger.error(f"[HealthCheck] failed: ArgoCD endpoint responded with unexpected status {response.status_code}")
            raise kopf.TemporaryError("[HealthCheck] failed: service unavailable", delay=on_failure_delay)
            
    except requests.exceptions.RequestException as e:
        logger.error(f"[HealthCheck] failed: Unexpected error: {str(e)}")
        raise kopf.TemporaryError("[HealthCheck] failed service unavailable", delay=on_failure_delay)
    

@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    # """disable event posting with logs"""
    settings.posting.enabled = False
    # settings.posting.level = logging.ERROR
    settings.persistence.finalizer = "hpa-prescaler.hepapi.com/kopf-finalizer"

       
@kopf.on.login()
def login_fn(**kwargs):
    """handles k8s authentication"""
    return kopf.login_with_service_account(**kwargs) or kopf.login_with_kubeconfig(**kwargs)

def create_kubernetes_event(namespace, event_type, regarding_prescaler_name, action, reason, note, logger):
    # Normal, Warning, Error
    if not DO_CREATE_KUBERNETES_EVENTS:
        logger.debug(f"Skipping creation of Kubernetes Event for {regarding_prescaler_name} as DO_CREATE_KUBERNETES_EVENTS is false")
        return 
    
    now = datetime.datetime.now(datetime.timezone.utc)
    event_body = kubernetes.client.EventsV1Event(
        metadata=kubernetes.client.V1ObjectMeta(
            generate_name="hpa-prescaler", namespace=namespace
        ),
        reason=reason,
        note=note,
        event_time=now,
        action=action,
        type=event_type,
        reporting_instance="hpa-prescaler-controller",
        reporting_controller="hpa-prescaler-controller",
        regarding=kubernetes.client.V1ObjectReference(
            kind="hpaprescaler", name=regarding_prescaler_name, namespace=namespace
        ),
    )
    try:
        api_response = events_api.create_namespaced_event(namespace, event_body)
        return api_response
    except ApiException as e:
        logger.error("Exception when creating K8s Event: %s\n" % e)
        return False


@kopf.on.delete('hpaprescalers')
def delete_hpaprescaler(name, spec, status, logger, **kwargs):
    # this function is needed for Finalizers to be removed correctly
    logger.info(f"Deleting HpaPrescaler object: {json.dumps({'name': name, 'spec': spec, 'status': status}, default=str)}")


@kopf.on.create('hpaprescalers')
def create_hpaprescaler(name, namespace, status, logger, **kwargs):
    """Sets the .status of the object to it's initial values"""
    _default_status = {'state': str(OP_STATE.PENDING.value), "message": "", "processedAt":""}
    if not status: # object status is not set
        success = update_status_of_prescaler_obj(name, namespace, _default_status, logger)
        if not success:
            raise kopf.TemporaryError(f"ERROR: can not patch .status of HpaPrescaler({name}), retrying in 5 seconds...", delay=5)
        logger.info(f"HpaPrescaler({name}) is created and it's .status is set.")
