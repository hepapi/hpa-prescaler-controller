from typing import Dict, Optional
import requests
import os 
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from enum import Enum
import json


ARGOCD_ENDPOINT = os.environ.get('ARGOCD_ENDPOINT')
ARGOCD_TOKEN = os.environ.get('ARGOCD_TOKEN')
ARGOCD_SSL_VERIFY = os.environ.get('ARGOCD_SSL_VERIFY', 'false').lower() == 'true'


class ArgoAppUpdateStatus(Enum):
    SUCCESS = 'success'
    ARGO_CONNECTION_FAILED = "Can't connect to ArgoCD Server"
    APP_NOT_FOUND = "Argo App not found"
    APP_NOT_UPDATED = "Argo App update failed"
    SYNC_FAILED = "Argo App Sync failed"
    NO_HELM_SOURCE = "Argo App has no usable helm source"
       
       
_headers = {
    'User-Agent': 'python-requests/2.32.3', 
    "Authorization": f"Bearer {ARGOCD_TOKEN}",
    "Content-Type": "application/json"
}

_cookies = { "argocd.token": ARGOCD_TOKEN }

if not ARGOCD_SSL_VERIFY:
    # Disable SSL warnings if SSL Verification is disabled
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)


_DEFAULT_AUTOSCALE_HELM_PARAMS = [
    {'name': 'autoscaling.enabled', 'value': 'true'},
    {'name': 'autoscaling.minReplicas', 'value': False},
    {'name': 'autoscaling.maxReplicas', 'value': False},
]

_AUTOSCALE_PARAM_NAMES = {
    'autoscaling.enabled',
    'autoscaling.minReplicas',
    'autoscaling.maxReplicas',
}


def get_argocd_app(app_name, logger):
    _get_app_endpoint=f"{ARGOCD_ENDPOINT}/api/v1/applications"
    qparams=f"?name={app_name}"
    try:
        response = requests.get(f"{_get_app_endpoint}{qparams}", headers=_headers, cookies=_cookies, verify=ARGOCD_SSL_VERIFY)
    except requests.exceptions.ConnectionError:
        logger.error(f"Connection Error when querying ArgoCD api for App({app_name})")
        return False, ArgoAppUpdateStatus.ARGO_CONNECTION_FAILED       

        
    if not response.ok:
        logger.error(f"Error when querying ArgoCD api for App({app_name}). Message: {response.text}")
        return False, ArgoAppUpdateStatus.APP_NOT_FOUND       
    response_json = response.json()

    argo_apps = response_json.get('items')
    if not argo_apps or len(argo_apps) != 1:
        return False, ArgoAppUpdateStatus.APP_NOT_FOUND       
    return argo_apps[0], ArgoAppUpdateStatus.SUCCESS


def _source_param_names(source: Dict) -> set:
    params = (source.get('helm') or {}).get('parameters') or []
    return {p.get('name') for p in params if p.get('name')}


def _is_ref_only_values_source(source: Dict) -> bool:
    """Values-repo source used only as `$ref` for valueFiles (no chart render)."""
    return bool(source.get('ref')) and not source.get('chart') and 'helm' not in source


def find_helm_source(app_spec: Optional[Dict]) -> Optional[Dict]:
    """
    Return the mutable source dict that should receive HPA helm parameters.

    Supports:
      - classic single-source apps: spec.source
      - multi-source apps: spec.sources[] (pick the actual helm/chart source,
        not a ref-only values repository)
    """
    if app_spec is None:
        return {}
    if not isinstance(app_spec, dict):
        return None

    sources = app_spec.get('sources')
    if isinstance(sources, list) and sources:
        # Prefer source that already carries autoscaling helm parameters
        for src in sources:
            if isinstance(src, dict) and _source_param_names(src) & _AUTOSCALE_PARAM_NAMES:
                return src

        # Prefer an explicit helm block or a chart source
        for src in sources:
            if isinstance(src, dict) and (src.get('helm') is not None or src.get('chart')):
                return src

        # Prefer a path-based source that is not ref-only (chart directory)
        for src in sources:
            if isinstance(src, dict) and src.get('path') and not _is_ref_only_values_source(src):
                return src

        # Last resort: first non ref-only source
        for src in sources:
            if isinstance(src, dict) and not _is_ref_only_values_source(src):
                return src

        return None

    # Classic single-source Application
    source = app_spec.get('source')
    if source is None:
        app_spec['source'] = {}
        return app_spec['source']
    if not isinstance(source, dict):
        return None
    return source


def _ensure_helm_parameters_on_source(app_name, source: Dict, logger) -> list:
    """Ensure source.helm.parameters exists (deduped, with default autoscale keys)."""
    has_helm_def = source.get('helm', False)
    has_helm_parameters_def = (source.get('helm') or {}).get('parameters', False) != False

    if has_helm_def:
        if has_helm_parameters_def:
            existing_parameters = source['helm']['parameters']

            # De-duplicate: keep only the first occurrence of each param name
            deduped_parameters = []
            seen_names = set()
            for _p in existing_parameters:
                _name = _p.get('name')
                if _name not in seen_names:
                    seen_names.add(_name)
                    deduped_parameters.append(_p)
            source['helm']['parameters'] = deduped_parameters
            existing_parameters = deduped_parameters

            existing_parameter_names = {p.get('name') for p in existing_parameters}
            for _a_helm_param in _DEFAULT_AUTOSCALE_HELM_PARAMS:
                if _a_helm_param['name'] not in existing_parameter_names:
                    source['helm']['parameters'].append(_a_helm_param)
        else:
            logger.info(f"ArgoApp({app_name}) DOES NOT HAVE helm.parameters definition, adding it now.")
            source['helm']['parameters'] = list(_DEFAULT_AUTOSCALE_HELM_PARAMS)
    else:
        logger.info(f"ArgoApp({app_name}) DOES NOT HAVE helm definition on selected source, adding it now.")
        source['helm'] = {'parameters': list(_DEFAULT_AUTOSCALE_HELM_PARAMS)}

    return source['helm']['parameters']


def _apply_hpa_values_to_parameters(app_name, helm_parameters: list, new_hpa_config, logger) -> None:
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
        logger.info(f"ArgoApp({app_name}) doesn't have autoscaling.minReplicas set, setting it to: {min_hpa_conf}")
    if not _done_max_replicas:
        helm_parameters.append({'name': 'autoscaling.maxReplicas', 'value': max_hpa_conf})
        logger.info(f"ArgoApp({app_name}) doesn't have autoscaling.maxReplicas set, setting it to: {max_hpa_conf}")


def _normalize_destination(app_name, app_spec: Dict, logger) -> None:
    # app_spec.destination -> should have only one server or name
    # otherwise ArgoCD API will error: 'spec is invalid: application destination can't have both name and server defined'
    if not isinstance(app_spec, dict):
        return
    destination = app_spec.get('destination') or {}
    if 'server' in destination and 'name' in destination:
        logger.info(f'ArgoApp({app_name}) .spec.destination has .server and .name defined in it. Removing .server definition.')
        destination.pop('server')
        logger.info(f"ArgoApp({app_name}) Updated .destination: {destination}")


def update_app_spec_with_new_hpa_config(app_name, app_spec: Optional[Dict], new_hpa_config, logger):
    if app_spec is None:
        app_spec = {}

    source = find_helm_source(app_spec)
    if source is None:
        logger.error(
            f"ArgoApp({app_name}) has spec.sources but no usable helm/chart source to update"
        )
        raise ValueError(f"ArgoApp({app_name}) has no usable helm source")

    sources = app_spec.get('sources') if isinstance(app_spec, dict) else None
    if isinstance(sources, list) and sources:
        logger.info(
            f"ArgoApp({app_name}) uses multi-source spec; updating helm source "
            f"repoURL={source.get('repoURL')} path={source.get('path')} chart={source.get('chart')}"
        )

    helm_parameters = _ensure_helm_parameters_on_source(app_name, source, logger)
    _apply_hpa_values_to_parameters(app_name, helm_parameters, new_hpa_config, logger)
    _normalize_destination(app_name, app_spec, logger)
    return app_spec

def update_argocd_app(app_name, new_hpa_config, logger):
    _app_spec_update_endpoint=f"{ARGOCD_ENDPOINT}/api/v1/applications/{app_name}/spec"
    logger.info(f"Updating ArgoCD App({app_name}) using api endpoint: {_app_spec_update_endpoint}")
    app_data, _get_app_status = get_argocd_app(app_name, logger)
    
    if _get_app_status != ArgoAppUpdateStatus.SUCCESS:
        logger.error(f"ArgoCD App({app_name}) is NOT FOUND. Does this app exists on ArgoCD?")
        return False, _get_app_status
    
    app_spec = app_data.get('spec') or {}
    try:
        new_app_spec = update_app_spec_with_new_hpa_config(app_name, app_spec, new_hpa_config, logger)
    except ValueError:
        return False, ArgoAppUpdateStatus.NO_HELM_SOURCE
    
    try:
        logger.debug(f"Updating ArgoCD App({app_name}) .spec with new HPA config: {json.dumps(new_app_spec)}")
        response = requests.put(_app_spec_update_endpoint, data=json.dumps(new_app_spec), headers=_headers, cookies=_cookies, verify=ARGOCD_SSL_VERIFY)
    except requests.exceptions.ConnectionError:
        logger.error(f"Failed to connect to ArgoCD API endpoint: {_app_spec_update_endpoint}")
        return False, ArgoAppUpdateStatus.ARGO_CONNECTION_FAILED       
    
    if not response.ok:
        logger.error(f"Failed to update ArgoCD App({app_name}). Status code: {response.status_code}, Response: {response.text}")
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
            sync_response = requests.post(_app_sync_endpoint, headers=_headers, cookies=_cookies, verify=ARGOCD_SSL_VERIFY)
        except requests.exceptions.ConnectionError:
            return False, ArgoAppUpdateStatus.ARGO_CONNECTION_FAILED       
        
        if not sync_response.ok:
            logger.error(f"Failed to SYNC ArgoCD App({app_name}).")
            return False, ArgoAppUpdateStatus.SYNC_FAILED
        logger.info(f"Triggered a SYNC ArgoCD App({app_name}) as it doesn't have auto-sync enabled.")
    else:
        logger.debug(f"Not triggering SYNC of ArgoCD App({app_name}) as it's already has auto-sync enabled.")
    return True, ArgoAppUpdateStatus.SUCCESS



# if __name__ == '__main__':
#     import logging
#     a =update_argocd_app('nginx1', {"minReplicas": 1, "maxReplicas": 9},logging.getLogger(__name__))
#     # a =update_argocd_app('nginx2', {"minReplicas": 1, "maxReplicas": 9},logging.getLogger(__name__))
#     a
