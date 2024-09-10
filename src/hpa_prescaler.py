
"""
- kopf sends everythin as event, disable that
- 
"""

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


# ---- Configs from Env Vars
LOOP_INTERVAL_SECS=int(os.environ.get('LOOP_INTERVAL_SECS'))
LOOP_INITAL_DELAY_SECS=int(os.environ.get('LOOP_INITAL_DELAY_SECS'))
GRACE_TIME_DELTA_MINS=int(os.environ.get('GRACE_TIME_DELTA_MINS'))
RELEASE_NAMESPACE=os.environ.get('RELEASE_NAMESPACE')
DEPLOY_ENV=os.environ.get('DEPLOY_ENV', "Development")

if DEPLOY_ENV.lower() in ('prod', 'production'):
    kubernetes.config.load_incluster_config()
else:
    kubernetes.config.load_kube_config()

api = kubernetes.client.CustomObjectsApi()
events_api = kubernetes.client.EventsV1Api()

class TimeStatus(Enum):
    PASSED = "passed"
    WITHIN_GRACE_WINDOW = "within_grace_window"
    NOT_STARTED = "not_started"

class OP_STATE(Enum):
    PENDING = 'PENDING'
    SUCCEEDED = 'SUCCEEDED'
    FAILED = 'FAILED'
    

@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    # """disable event posting with logs"""
    settings.posting.enabled = False
    # settings.posting.level = logging.ERROR
       
@kopf.on.login()
def login_fn(**kwargs):
    """handles k8s authentication"""
    return kopf.login_with_service_account(**kwargs) or kopf.login_with_kubeconfig(**kwargs)

def create_kubernetes_event(namespace, event_type, regarding_prescaler_name, action, reason, note, logger):
    # Normal, Warning, Error
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


def check_time_status(target_time_iso8601, grace_minutes=GRACE_TIME_DELTA_MINS) -> TimeStatus:
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
        # raise kopf.TemporaryError(f"ERROR: can not patch .status of HpaPrescaler({name})", delay=30)

@kopf.on.create('hpaprescalers')
def create_hpaprescaler(name, namespace, status, logger, **kwargs):
    """Sets the .status of the object to it's initial values"""
    _default_status = {'state': str(OP_STATE.PENDING.value), "message": "", "processedAt":""}
    if not status: # object status is not set
        success = update_status_of_prescaler_obj(name, namespace, _default_status, logger)
        if not success:
            raise kopf.TemporaryError(f"ERROR: can not patch .status of HpaPrescaler({name})", delay=20)
        logger.info(f"HpaPrescaler({name}) is created and it's .status is set.")

@kopf.daemon('hpaprescalers', initial_delay=LOOP_INITAL_DELAY_SECS)
async def monitor_hpa_prescalers(stopped, logger, name, namespace, status, spec, **kwargs):
    """Runs for each HpaPrescaler object, and waits for some time..."""
    api = kubernetes.client.CustomObjectsApi()
    utc_current_time_str = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    prescaler_name = f"HpaPrescaler({namespace}/{name})"

    while not stopped:
        if not status:
            logger.warn(f"{prescaler_name} doesn't have a .status, skipping for now...")
            _non_status_delay_secs = 30
            raise kopf.TemporaryError(
                f"{prescaler_name} doesn't have a .status, skipping for {_non_status_delay_secs} seconds...", 
                delay=_non_status_delay_secs)

        timeStart = spec['timeStart']
        argocdAppName = spec['argocdAppName']
        
        if status.get('state') != OP_STATE.PENDING.value:
            # already processed
            logger.debug(f"Skipping {prescaler_name} as it's already processed...")
            return {'lastCheckedAt': utc_current_time_str}

        # must pending CRD
        assert status.get('state') == OP_STATE.PENDING.value
        
        time_status: TimeStatus = check_time_status(timeStart, GRACE_TIME_DELTA_MINS)
        
        if time_status == TimeStatus.PASSED:
            logger.error(f"Target time for {prescaler_name} has passed.")
            create_kubernetes_event(RELEASE_NAMESPACE, 'Warning', name, 'ProcessPrescaler', 'ErrorTimeAlreadyPassed', f"Target time for {prescaler_name} has passed.", logger)
            _time_passed_status = {'state': OP_STATE.FAILED.value, "message": "Time Passed", "processedAt":utc_current_time_str} 
            _success = update_status_of_prescaler_obj(name, namespace, _time_passed_status, logger)
            if not _success:
                logger.warn(f"Can not patch .status of {prescaler_name}")
                raise kopf.TemporaryError(f"ERROR: can not patch .status of {prescaler_name}", delay=30)
            
        elif time_status == TimeStatus.WITHIN_GRACE_WINDOW:
            logger.info(f"ACCEPTED {prescaler_name} as it's target time is withing GraceWindow({GRACE_TIME_DELTA_MINS} mins).")

            _update_success, _app_update_status = update_hpa_of_argocd_app(name, namespace, spec, logger)

            # update .status of HpaPrescaler Object
            _updated_time_str = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            if _update_success:
                _succeeded_status = {'state': OP_STATE.SUCCEEDED.value, "message": "OK", "processedAt":_updated_time_str}
                
                if not update_status_of_prescaler_obj(name, namespace, _succeeded_status, logger):
                    logger.error(f"Failed to update .status of HpaPrescaler({name}) obj to: {json.dumps(_succeeded_status)}")
                    create_kubernetes_event(RELEASE_NAMESPACE, 'Warning', name, 'UpdatePrescalerStatus', 'FailUpdatePrescalerStatus', f"Failed to update ArgocdApp({argocdAppName}) with updated status. Failure: {_app_update_status.value}", logger)

                    
                    raise kopf.TemporaryError(f"ERROR: can not patch .status of {prescaler_name}", delay=30)

                create_kubernetes_event(RELEASE_NAMESPACE, 'Normal', name, 'ProcessPrescaler', 'SuccessfullyUpdatedHPA', f"Succeeded to update the HPA of ArgocdApp({argocdAppName}) with HpaPrescaler({name})", logger)
                return {'lastCheckedAt': utc_current_time_str}  # stop monitoring this obj 
            else:
                # within grace window, but something went wrong with ArgoCD communication
                logger.error(f"Failed to update ArgocdApp({argocdAppName}) with updated status. Failure: {_app_update_status}")
                _argo_issue_status = {'state': OP_STATE.FAILED.value, "message": _app_update_status.value, "processedAt":_updated_time_str}
                update_status_of_prescaler_obj(name, namespace, _argo_issue_status, logger)
                create_kubernetes_event(RELEASE_NAMESPACE, 'Warning', name, 'UpdateArgoAppHPA', 'FailToUpdateArgoAppHPA', f"Failed to update ArgocdApp({argocdAppName}) with updated status. Failure: {_app_update_status}", logger)

        elif time_status == TimeStatus.NOT_STARTED:
            logger.debug(f"Skipping {prescaler_name} as it's in future.")
        else:
            logger.error(f"Unchecked kind of TimeStatus: {time_status}")
        await stopped.wait(LOOP_INTERVAL_SECS)
    return {'lastCheckedAt': utc_current_time_str}
        
        
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
        logger.error(f"Failed to get HpaScalerProfiles from api-server. Did you create any profiles?")
        create_kubernetes_event(RELEASE_NAMESPACE, 'Warning', name, 'GetPrescalerProfiles', 'FailToListPrescalerProfiles', f"Failed to get HpaScalerProfiles from api-server. Did you create any profiles?", logger)

        raise kopf.TemporaryError(f"Failed to get HpaScalerProfiles from api-server.", delay=30)

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
