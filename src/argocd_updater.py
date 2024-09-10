from typing import Dict
import requests
import os 
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from enum import Enum
import json


ARGOCD_ENDPOINT = os.environ.get('ARGOCD_ENDPOINT', 'default-argocd-endpoint')
ARGOCD_TOKEN = os.environ.get('ARGOCD_TOKEN','default-argocd-api-token')
ARGOCD_SSL_VERIFY = os.environ.get('ARGOCD_SSL_VERIFY', 'false').lower() == 'true'


class ArgoAppUpdateStatus(Enum):
    SUCCESS = 'success'
    ARGO_CONNECTION_FAILED = "Can't connect to ArgoCD Server"
    APP_NOT_FOUND = "Argo App not found"
    APP_NOT_UPDATED = "Argo App update failed"
    SYNC_FAILED = "Argo App Sync failed"
       
       
_headers = {
    'User-Agent': 'python-requests/2.32.3', 
    "Authorization": f"Bearer {ARGOCD_TOKEN}",
    "Content-Type": "application/json"
}

if not ARGOCD_SSL_VERIFY:
    # Disable SSL warnings if SSL Verification is disabled
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)


def get_argocd_app(app_name):
    _get_app_endpoint=f"{ARGOCD_ENDPOINT}/api/v1/applications"
    qparams=f"?name={app_name}"
    try:
        response = requests.get(f"{_get_app_endpoint}{qparams}", headers=_headers, verify=ARGOCD_SSL_VERIFY)
    except requests.exceptions.ConnectionError:
        return False, ArgoAppUpdateStatus.ARGO_CONNECTION_FAILED       
        
    if not response.ok:
        return False, ArgoAppUpdateStatus.APP_NOT_FOUND       
    response_json = response.json()

    argo_apps = response_json.get('items')
    if not argo_apps or len(argo_apps) != 1:
        return False, ArgoAppUpdateStatus.APP_NOT_FOUND       
    return argo_apps[0], ArgoAppUpdateStatus.SUCCESS


def update_app_spec_with_new_hpa_config(app_name, app_spec: Dict, new_hpa_config,logger):
    helm_parameters = app_spec['source']['helm']['parameters']
    _done_max_replicas = False
    _done_min_replicas = False
    
    min_hpa_conf = str(new_hpa_config['minReplicas'])
    max_hpa_conf = str(new_hpa_config['maxReplicas'])
    
    for helm_p in helm_parameters:
        if helm_p['name'] == 'autoscaling.maxReplicas':
            helm_p['value'] = max_hpa_conf 
            _done_max_replicas = True
    
        if helm_p['name'] == 'autoscaling.minReplicas':
            helm_p['value'] = min_hpa_conf
            _done_min_replicas = True
    
    if not _done_min_replicas:
        helm_parameters.append({'name': 'autoscaling.minReplicas', 'value': min_hpa_conf})
        logger.debug(f"ArgoApp({app_name}) doesn't have autoscaling.minReplicas set, setting it to: {min_hpa_conf}")
    
    if not _done_min_replicas:
        helm_parameters.append({'name': 'autoscaling.maxReplicas', 'value': max_hpa_conf})
        logger.debug(f"ArgoApp({app_name}) doesn't have autoscaling.maxReplicas set, setting it to: {max_hpa_conf}")

    return app_spec


def update_argocd_app(app_name, new_hpa_config, logger):
    _app_spec_update_endpoint=f"{ARGOCD_ENDPOINT}/api/v1/applications/{app_name}/spec"
    app_data, _get_app_status = get_argocd_app(app_name)
    
    if _get_app_status != ArgoAppUpdateStatus.SUCCESS:
        logger.error(f"ArgoCD App({app_name}) is NOT FOUND. Does this app exists on ArgoCD?")
        return False, _get_app_status
    
    app_spec = app_data.get('spec')
    new_app_spec = update_app_spec_with_new_hpa_config(app_name, app_spec, new_hpa_config, logger)
    
    try:
        response = requests.put(_app_spec_update_endpoint, data=json.dumps(new_app_spec), headers=_headers, verify=ARGOCD_SSL_VERIFY)
    except requests.exceptions.ConnectionError:
        return False, ArgoAppUpdateStatus.ARGO_CONNECTION_FAILED       
    
    if not response.ok:
        logger.error(f"Failed to update ArgoCD App({app_name}).")
        return False, ArgoAppUpdateStatus.APP_NOT_UPDATED

    updated_spec = response.json()
    
    logger.info(f"Successfuly updated ArgoCD App({app_name}) HPA definitions.")
    logger.debug(f"Successfuly updated ArgoCD App({app_name}). Updated .spec: {json.dumps(updated_spec)}")

    # Send a sync request if auto-sync is not enabled 
    sync_policy = app_spec.get('syncPolicy', {})
    _has_autosync_enabled = 'automated' in sync_policy

    if not _has_autosync_enabled:
        # auto sync is disabled, do a sync     
        _app_sync_endpoint=f"{ARGOCD_ENDPOINT}/api/v1/applications/{app_name}/sync"
        try:
            sync_response = requests.post(_app_sync_endpoint, headers=_headers, verify=ARGOCD_SSL_VERIFY)
        except requests.exceptions.ConnectionError:
            return False, ArgoAppUpdateStatus.ARGO_CONNECTION_FAILED       
        
        sync_response
        if not sync_response.ok:
            logger.error(f"Failed to SYNC ArgoCD App({app_name}).")
            return False, ArgoAppUpdateStatus.SYNC_FAILED
        logger.info(f"Triggered a SYNC ArgoCD App({app_name}) as it doesn't have auto-sync enabled.")
    else:
        logger.debug(f"Not triggering SYNC of ArgoCD App({app_name}) as it's already has auto-sync enabled.")
    return True, ArgoAppUpdateStatus.SUCCESS



if __name__ == '__main__':
    import logging
    a =update_argocd_app('nginx1', {"minReplicas": 1, "maxReplicas": 9},logging.getLogger(__name__))
    a
