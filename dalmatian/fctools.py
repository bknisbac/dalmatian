# Author: Francois Aguet

import os, io, json
import subprocess
from datetime import datetime
from collections import Iterable
import pandas as pd
import numpy as np
import firecloud.api
import iso8601
import pytz
import matplotlib.pyplot as plt

import argparse

# Collection of high-level wrapper functions for FireCloud API

def convert_time(x):
    return datetime.timestamp(iso8601.parse_date(x))


def workflow_time(workflow):
    """
    Convert API output to timestamp difference
    """
    if 'end' in workflow:
        return convert_time(workflow['end']) - convert_time(workflow['start'])
    else:
        return np.NaN


class WorkspaceManager:
    def __init__(self, namespace, workspace, timezone='America/New_York'):
        self.namespace = namespace
        self.workspace = workspace
        self.timezone  = timezone


    def create_workspace(self):
        """
        Wrapper for firecloud.api.create_workspace
        """
        r = firecloud.api.create_workspace(self.namespace, self.workspace)
        if r.status_code==201:
            print('Workspace {}/{} successfully created.'.format(self.namespace, self.workspace))
        elif r.status_code==409:
            print(r.json()['message'])
        else:
            print(r.json())


    def delete_workspace(self):
        r = firecloud.api.delete_workspace(self.namespace, self.workspace)
        assert r.status_code==202
        print('Workspace {}/{} successfully deleted.'.format(self.namespace, self.workspace))
        print('  * '+r.json()['message'])


    def upload_samples(self, df, participant_df=None):
        """
        Upload samples stored in a pandas DataFrame, and populate the required
        participant, sample, and sample_set attributes

        df columns: sample_id, participant_id, {sample_set_id,} other attributes
        """
        assert df.index.name=='sample_id' and df.columns[0]=='participant_id'

        # 1) upload participant IDs (without additional attributes)
        if participant_df is None:
            participant_ids = np.unique(df['participant_id'])
            participant_df = pd.DataFrame(data=participant_ids, columns=['entity:participant_id'])
        else:
            assert participant_df.index.name=='entity:participant_id' or participant_df.columns[0]=='entity:participant_id'

        buf = io.StringIO()
        participant_df.to_csv(buf, sep='\t', index=participant_df.index.name=='entity:participant_id')
        s = firecloud.api.upload_entities_tsv(self.namespace, self.workspace, buf)
        buf.close()
        assert s.status_code==200
        print('Succesfully imported participants.')

        # 2) upload samples
        sample_df = df[df.columns[df.columns!='sample_set_id']].copy()
        sample_df.index.name = 'entity:sample_id'
        buf = io.StringIO()
        sample_df.to_csv(buf, sep='\t')
        s = firecloud.api.upload_entities_tsv(self.namespace, self.workspace, buf)
        buf.close()
        assert s.status_code==200
        print('Succesfully imported samples.')

        # 3 upload sample sets
        if 'sample_set_id' in df.columns:
            set_df = pd.DataFrame(data=sample_df.index.values, columns=['sample_id'])
            set_df.index = df['sample_set_id']
            set_df.index.name = 'membership:sample_set_id'
            buf = io.StringIO()
            set_df.to_csv(buf, sep='\t')
            s = firecloud.api.upload_entities_tsv(self.namespace, self.workspace, buf)
            buf.close()
            assert s.status_code==200
            print('Succesfully imported sample sets.')

        # 4) add participant.samples_
        print('  * The FireCloud data model currently does not provide participant.samples\n    Adding "participant.samples_" as an explicit attribute.')
        self.update_participant_samples()


    def upload_participants(self, participant_ids):
        """
        Upload samples stored in a pandas DataFrame, and populate the required
        participant, sample, and sample_set attributes

        df columns: sample_id, participant_id, {sample_set_id,} other attributes
        """
        participant_df = pd.DataFrame(data=np.unique(participant_ids), columns=['entity:participant_id'])
        buf = io.StringIO()
        participant_df.to_csv(buf, sep='\t', index=participant_df.index.name=='entity:participant_id')
        s = firecloud.api.upload_entities_tsv(self.namespace, self.workspace, buf)
        buf.close()
        assert s.status_code==200
        print('Succesfully imported participants.')


    def update_participant_samples(self):
        """
        Attach samples to participants
        """
        df = self.get_samples()[['participant']]
        samples_dict = {k:g.index.values for k,g in df.groupby('participant')}

        participant_ids = np.unique(df['participant'])
        for j,k in enumerate(participant_ids):
            print('\r    Updating samples for participant {}/{}'.format(j+1,len(participant_ids)), end='')
            attr_dict = {
                "samples_": {
                    "itemsType": "EntityReference",
                    "items": [{"entityType": "sample", "entityName": i} for i in samples_dict[k]]
                }
            }
            attrs = [firecloud.api._attr_set(i,j) for i,j in attr_dict.items()]
            r = firecloud.api.update_entity(self.namespace, self.workspace, 'participant', k, attrs)
            assert r.status_code==200
        print('\n    Finished updating participants in {}/{}'.format(self.namespace, self.workspace))


    def update_attributes(self, attr_dict):
        """
        Wrapper for API 'set' call
        """
        attrs = [firecloud.api._attr_set(i,j) for i,j in attr_dict.items()]
        r = firecloud.api.update_workspace_attributes(self.namespace, self.workspace, attrs)  # attrs must be list
        assert r.status_code==200
        print('Successfully updated workspace attributes in {}/{}'.format(self.namespace, self.workspace))


    def get_submission_status(self, configuration):
        """
        Get status of lastest submission for samples in the workspace

        Columns: status (Suceeded, Failed, Submitted), submission timestamp, submission ID
        """

        # get submissions
        submissions = firecloud.api.list_submissions(self.namespace, self.workspace)
        assert submissions.status_code==200
        submissions = submissions.json()

        # filter by configuration
        submissions = [s for s in submissions if configuration in s['methodConfigurationName']]

        # get status of last run submission
        sample_dict = {}
        for s in submissions:
            r = firecloud.api.get_submission(self.namespace, self.workspace, s['submissionId'])
            assert r.status_code==200
            r = r.json()

            ts = datetime.timestamp(iso8601.parse_date(s['submissionDate']))
            if s['submissionEntity']['entityType']=='sample':
                sample_id = s['submissionEntity']['entityName']
                if sample_id not in sample_dict or sample_dict[sample_id]['timestamp']<ts:
                    status = r['workflows'][0]['status']
                    sample_dict[sample_id] = {'status':status, 'timestamp':ts, 'submission_id':s['submissionId'], 'configuration':s['methodConfigurationName'], 'workflow_id':r['workflows'][0]['workflowId']}
            elif s['submissionEntity']['entityType']=='sample_set':
                if len(r['workflows'])==1:
                    sample_id = s['submissionEntity']['entityName']
                    if sample_id not in sample_dict or sample_dict[sample_id]['timestamp']<ts:
                        status = r['workflows'][0]['status']
                        sample_dict[sample_id] = {'status':status, 'timestamp':ts, 'submission_id':s['submissionId'], 'configuration':s['methodConfigurationName'], 'workflow_id':r['workflows'][0]['workflowId']}
                else:
                    for w in r['workflows']:
                        sample_id = w['workflowEntity']['entityName']
                        if sample_id not in sample_dict or sample_dict[sample_id]['timestamp']<ts:
                            sample_dict[sample_id] = {'status':w['status'], 'timestamp':ts, 'submission_id':s['submissionId'], 'configuration':s['methodConfigurationName'], 'workflow_id':w['workflowId']}
            elif s['submissionEntity']['entityType']=='participant':
                participant_id = s['submissionEntity']['entityName']
                if participant_id not in sample_dict or sample_dict[participant_id]['timestamp']<ts:
                    status = r['workflows'][0]['status']
                    sample_dict[participant_id] = {'status':status, 'timestamp':ts, 'submission_id':s['submissionId'], 'configuration':s['methodConfigurationName'], 'workflow_id':r['workflows'][0]['workflowId']}
                # print(participant_id)

        status_df = pd.DataFrame(sample_dict).T
        status_df.index.name = 'sample_id'

        print(status_df['status'].value_counts())
        return status_df[['status', 'timestamp', 'workflow_id', 'submission_id', 'configuration']]


    def display_status(self, configuration, entity='sample'):
        """
        """
        status_df = self.get_submission_status(configuration)

        # get workflow details from 1st submission
        metadata = firecloud.api.get_workflow_metadata(self.namespace, self.workspace, status_df['submission_id'][0], status_df['workflow_id'][0])
        assert metadata.status_code==200
        metadata = metadata.json()

        workflow_tasks = list(metadata['calls'].keys())

        fail_idx = status_df[status_df['status']!='Succeeded'].index
        n_success = status_df.shape[0] - len(fail_idx)

        state_df = pd.DataFrame(0, index=fail_idx, columns=workflow_tasks)
        status_code = {'Running':1, 'Done':2, 'Failed':-1}
        for i in fail_idx:
            metadata = firecloud.api.get_workflow_metadata(self.namespace, self.workspace, status_df.loc[i, 'submission_id'], status_df.loc[i, 'workflow_id'])
            assert metadata.status_code==200
            metadata = metadata.json()
            state_df.loc[i] = [status_code[metadata['calls'][t][-1]['executionStatus']] if t in metadata['calls'] else 0 for t in workflow_tasks]
        state_df.rename(columns={i:i.split('.')[1] for i in state_df.columns}, inplace=True)
        state_df[['workflow_id', 'submission_id']] = status_df.loc[fail_idx, ['workflow_id', 'submission_id']]

        return state_df


    def get_stderr(self, state_df, task_name):
        """
        Fetch stderrs from bucket (returns list of str)
        """
        df = state_df[state_df[task_name]==-1]
        fail_idx = df.index
        stderrs = []
        for n,i in enumerate(fail_idx):
            print('\rFetching stderr for task {}/{}'.format(n+1, len(fail_idx)), end='\r')
            metadata = firecloud.api.get_workflow_metadata(self.namespace, self.workspace, state_df.loc[i, 'submission_id'], state_df.loc[i, 'workflow_id'])
            assert metadata.status_code==200
            metadata = metadata.json()
            stderr_path = metadata['calls'][[i for i in metadata['calls'].keys() if i.split('.')[1]==task_name][0]][-1]['stderr']
            s = subprocess.check_output('gsutil cat '+stderr_path, shell=True, executable='/bin/bash').decode()
            stderrs.append(s)
        return stderrs


    def get_submission_history(self, configuration, sample_id):
        """
        Currently only supports samples
        """

        # get submissions
        submissions = firecloud.api.list_submissions(self.namespace, self.workspace)
        assert submissions.status_code==200
        submissions = submissions.json()

        # filter by configuration
        submissions = [s for s in submissions if configuration in s['methodConfigurationName']]

        # filter by sample
        submissions = [s for s in submissions if s['submissionEntity']['entityName']==sample_id and 'Succeeded' in list(s['workflowStatuses'].keys())]

        outputs_df = []
        for s in submissions:
            r = firecloud.api.get_submission(self.namespace, self.workspace, s['submissionId'])
            assert r.status_code==200
            r = r.json()

            metadata = firecloud.api.get_workflow_metadata(self.namespace, self.workspace, s['submissionId'], r['workflows'][0]['workflowId'])
            assert metadata.status_code==200
            metadata = metadata.json()

            outputs_s = pd.Series(metadata['outputs'])
            outputs_s.index = [i.split('.',1)[1].replace('.','_') for i in outputs_s.index]
            outputs_s['submission_date'] = iso8601.parse_date(s['submissionDate']).strftime('%H:%M:%S %m/%d/%Y')
            outputs_df.append(outputs_s)

        outputs_df = pd.concat(outputs_df, axis=1).T
        # sort by most recent first
        outputs_df = outputs_df.iloc[np.argsort([datetime.timestamp(iso8601.parse_date(s['submissionDate'])) for s in submissions])[::-1]]
        outputs_df.index = ['run_{}'.format(str(i)) for i in np.arange(outputs_df.shape[0],0,-1)]

        return outputs_df


    def get_storage(self):
        """
        Get total amount of storage used, in TB

        Pricing: $0.026/GB/month (multi-regional)
                 $0.02/GB/month (regional)
        """
        r = firecloud.api.get_workspace(self.namespace, self.workspace)
        assert r.status_code==200
        r = r.json()
        s = subprocess.check_output('gsutil du -s gs://'+r['workspace']['bucketName'], shell=True, executable='/bin/bash')
        return np.float64(s.decode().split()[0])/1024**4


    def get_stats(self, status_df, workflow_name=None):
        """
        For a list of submissions, calculate time, preemptions, etc
        """
        # for successful jobs, get metadata and count attempts
        status_df = status_df[status_df['status']=='Succeeded'].copy()
        metadata_dict = {}
        for k,(i,row) in enumerate(status_df.iterrows()):
            print('\rFetching metadata {}/{}'.format(k+1,status_df.shape[0]), end='')
            metadata = firecloud.api.get_workflow_metadata(self.namespace, self.workspace, row['submission_id'], row['workflow_id'])
            assert metadata.status_code==200
            metadata_dict[i] = metadata.json()

        # if workflow_name is None:
            # split output by workflow
        workflows = np.array([metadata_dict[k]['workflowName'] for k in metadata_dict])
        # else:
            # workflows = np.array([workflow_name])

        # get tasks for each workflow
        for w in np.unique(workflows):
            workflow_status_df = status_df[workflows==w]
            tasks = np.sort(list(metadata_dict[workflow_status_df.index[0]]['calls'].keys()))
            task_names = [t.rsplit('.')[-1] for t in tasks]

            task_dfs = {}
            for t in tasks:
                task_name = t.rsplit('.')[-1]
                task_dfs[task_name] = pd.DataFrame(index=workflow_status_df.index, columns=['time_h', 'total_time_h', 'max_preempt_time_h', 'machine_type', 'attempts', 'start_time', 'est_cost', 'job_ids'])
                for i in workflow_status_df.index:
                    task_dfs[task_name].loc[i, 'time_h'] = workflow_time(metadata_dict[i]['calls'][t][-1])/3600
                    exec_times_h = [workflow_time(t_attempt)/3600 for t_attempt in metadata_dict[i]['calls'][t]]
                    task_dfs[task_name].loc[i, 'total_time_h'] = np.sum(exec_times_h)

                    task_dfs[task_name].loc[i, 'machine_type'] = metadata_dict[i]['calls'][t][-1]['jes']['machineType'].rsplit('/')[-1]  # last instance

                    was_preemptible = [j['preemptible'] for j in metadata_dict[i]['calls'][t]]
                    if was_preemptible[0]:
                        task_dfs[task_name].loc[i, 'attempts'] = len(metadata_dict[i]['calls'][t])
                    # else:
                    #     task_dfs[task_name].loc[i, 'attempts'] = -1

                    if task_dfs[task_name].loc[i, 'attempts']>1:
                        task_dfs[task_name].loc[i, 'max_preempt_time_h'] = np.max([workflow_time(t_attempt) for t_attempt in metadata_dict[i]['calls'][t][:-1]])/3600

                    task_dfs[task_name].loc[i, 'start_time'] = iso8601.parse_date(metadata_dict[i]['calls'][t][-1]['start']).astimezone(pytz.timezone(self.timezone)).strftime('%H:%M')

                    machine_types = [j['jes']['machineType'].rsplit('/')[-1] for j in metadata_dict[i]['calls'][t]]
                    task_dfs[task_name].loc[i, 'est_cost'] = np.sum([get_vm_cost(m,p)*h for h,m,p in zip(exec_times_h, machine_types, was_preemptible)])

                    task_dfs[task_name].loc[i, 'job_ids'] = ','.join([j['jobId'] for j in  metadata_dict[i]['calls'][t]])

            # add overall cost
            workflow_status_df['est_cost'] = pd.concat([task_dfs[t.rsplit('.')[-1]]['est_cost'] for t in tasks], axis=1).sum(axis=1)
            workflow_status_df['time_h'] = [workflow_time(metadata_dict[i])/3600 for i in workflow_status_df.index]
            workflow_status_df['cpu_hours'] = pd.concat([task_dfs[t.rsplit('.')[-1]]['total_time_h'] * task_dfs[t.rsplit('.')[-1]]['machine_type'].apply(lambda i: int(i.rsplit('-',1)[-1])) for t in tasks], axis=1).sum(axis=1)
            workflow_status_df['start_time'] = [iso8601.parse_date(metadata_dict[i]['start']).astimezone(pytz.timezone(self.timezone)).strftime('%H:%M') for i in workflow_status_df.index]

        return workflow_status_df, task_dfs


    def publish_config(self, from_cnamespace, from_config, to_cnamespace, to_config, public=False):
        """
        copy configuration to repository
        """
        # check whether prior version exists
        r = get_config(to_cnamespace, to_config)
        old_version = None
        if r:
            old_version = np.max([m['snapshotId'] for m in r])
            print('Configuration {}/{} exists. SnapshotID: {}'.format(
                to_cnamespace, to_config, old_version))

        # copy config to repo
        r = firecloud.api.copy_config_to_repo(self.namespace, self.workspace, from_cnamespace, from_config, to_cnamespace, to_config)
        assert r.status_code==200
        print("Successfully copied {}/{}. New SnapshotID: {}".format(to_cnamespace, to_config, r.json()['snapshotId']))

        # make configuration public
        if public:
            print('  * setting public read access.')
            r = firecloud.api.update_repository_config_acl(to_cnamespace, to_config, r.json()['snapshotId'], [{'role': 'READER', 'user': 'public'}])

        # delete old version
        if old_version is not None:
            r = firecloud.api.delete_repository_config(to_cnamespace, to_config, old_version)
            assert r.status_code==200
            print("Successfully deleted SnapshotID {}.".format(old_version))


    def get_samples(self):
        """
        Get DataFrame with samples and their attributes
        """
        t = firecloud.api.get_entities_tsv(self.namespace, self.workspace, 'sample')
        assert t.status_code==200
        return pd.read_csv(io.StringIO(t.text), index_col=0, sep='\t')


    def get_sample_sets(self):
        """
        Get DataFrame with sample sets and their attributes
        """
        t = firecloud.api.get_entities(self.namespace, self.workspace, 'sample_set')
        assert t.status_code==200
        t = t.json()

        sample_set_ids = [i['name'] for i in t]
        columns = list(t[0]['attributes'].keys())
        df = pd.DataFrame(index=sample_set_ids, columns=columns)
        for s in t:
            for c in columns:
                if c in s['attributes']:
                    if isinstance(s['attributes'][c], dict):
                        df.loc[s['name'], c] = [i['entityName'] for i in s['attributes'][c]['items']]
                    else:
                        df.loc[s['name'], c] = s['attributes'][c]
        return df


    def update_sample_set(self, sample_set_id, sample_ids):
        """
        Update (or create) a sample set
        """
        r = firecloud.api.get_entity(self.namespace, self.workspace, 'sample_set', sample_set_id)
        if r.status_code==200:  # exists -> update
            items_dict = r['attributes']['samples']
            items_dict['items'] = [{'entityName': i, 'entityType': 'sample'} for i in sample_ids]
            attrs = [{'addUpdateAttribute': items_dict, 'attributeName': 'samples', 'op': 'AddUpdateAttribute'}]
            r = firecloud.api.update_entity(self.namespace, self.workspace, 'sample_set', sample_set_id, attrs)
            assert r.status_code==200
            print('Sample set "{}" successfully updated.'.format(sample_set_id))
        else:  # create
            set_df = pd.DataFrame(data=np.c_[[sample_set_id]*len(sample_ids), sample_ids], columns=['membership:sample_set_id', 'sample_id'])
            buf = io.StringIO()
            set_df.to_csv(buf, sep='\t', index=False)
            r = firecloud.api.upload_entities(self.namespace, self.workspace, buf.getvalue())
            buf.close()
            assert r.status_code==200
            print('Sample set "{}" successfully created.'.format(sample_set_id))


    def delete_sample_set(self, sample_set_id):
        """
        Delete sample set
        """
        r = firecloud.api.delete_sample_set(self.namespace, self.workspace, sample_set_id)
        assert r.status_code==204
        print('Sample set "{}" successfully deleted.'.format(sample_set_id))


    def update_configuration(self, json_body):
        """
        Create or update a method configuration (separate API calls)

        json_body = {
           'namespace': config_namespace,
           'name': config_name,
           'rootEntityType' : entity,
           'methodRepoMethod': {'methodName':method_name, 'methodNamespace':method_namespace, 'methodVersion':version},
           'methodNamespace': method_namespace,
           'methodConfigVersion':1,
           'inputs':  {},
           'outputs': {},
           'prerequisites': {},
           'deleted': False
        }

        """
        r = firecloud.api.list_workspace_configs(self.namespace, self.workspace)
        if json_body['name'] not in [m['name'] for m in r.json()]:
            # configuration doesn't exist -> name, namespace specified in json_body
            r = firecloud.api.create_workspace_config(self.namespace, self.workspace, json_body)
            assert r.status_code==201
            print('Successfully added configuration: {}'.format(json_body['name']))
        else:
            r = firecloud.api.update_workspace_config(self.namespace, self.workspace, json_body['namespace'], json_body['name'], json_body)
            assert r.status_code==200
            print('Successfully updated configuration: {}'.format(json_body['name']))
        return r


    def check_configuration(self, config_name):
        """
        Get version of a configuration and compare to latest available in repository
        """
        r = firecloud.api.list_workspace_configs(self.namespace, self.workspace)
        assert r.status_code==200
        r = r.json()
        r = [i for i in r if i['name']==config_name][0]
        # method repo version
        mrversion = get_method_version(r['methodRepoMethod']['methodNamespace'], r['methodRepoMethod']['methodName'])
        print('Method for config. {0}: {1} version {2} (latest: {3})'.format(config_name, r['methodRepoMethod']['methodName'], r['methodRepoMethod']['methodVersion'], mrversion))
        return r['methodRepoMethod']['methodVersion']


#------------------------------------------------------------------------------
# Functions for parsing Google metadata
#------------------------------------------------------------------------------
def get_google_metadata(job_id):
    """
    jobid: operations ID
    """
    if isinstance(job_id, str):
        s = subprocess.check_output('gcloud alpha genomics operations describe '+job_id+' --format json', shell=True, executable='/bin/bash')
        return json.loads(s.decode())
    elif isinstance(job_id, Iterable):
        json_list = []
        for k,j in enumerate(job_id):
            print('\rFetching metadata ({}/{})'.format(k+1,len(job_id)), end='')
            s = subprocess.check_output('gcloud alpha genomics operations describe '+j+' --format json', shell=True, executable='/bin/bash')
            json_list.append(json.loads(s.decode()))
        return json_list


def parse_google_stats(json_list):
    """
    Parse job start and end times, machine type, and preemption status from Google metadata
    """
    df = pd.DataFrame(index=[j['name'] for j in json_list], columns=['time_h', 'machine_type', 'preemptible', 'preempted'])
    for j in json_list:
        event_dict = {k['description']:convert_time(k['startTime']) for k in j['metadata']['events'] if 'copied' not in k}
        event_times = [convert_time(k['startTime']) for k in j['metadata']['events'] if 'copied' not in k]
        time_delta = np.max(event_times) - np.min(event_times)
        # if 'ok' in event_dict:
        #     time_delta = convert_time(event_dict['ok']) - convert_time(event_dict['start'])
        # elif 'start-shutdown' in event_dict:
        #     time_delta = convert_time(event_dict['start-shutdown']) - convert_time(event_dict['start'])
        # else:
        #     raise ValueError('unknown event')
        mt = j['metadata']['runtimeMetadata']['computeEngine']['machineType'].split('/')[-1]
        p = j['metadata']['request']['ephemeralPipeline']['resources']['preemptible']
        # j[0]['metadata']['request']['pipelineArgs']['resources']['preemptible']
        df.loc[j['name'], ['time_h', 'machine_type', 'preemptible', 'preempted']] = [time_delta/3600, mt, p, 'ok' not in event_dict]
    return df


def calculate_google_cost(jobid, jobid_lookup_df):
    """
    Calculate cost
    """
    r = jobid_lookup_df.loc[jobid]
    if r['preempted'] and r['time_h']<1/6:
        return 0
    else:
        return r['time_h']*get_vm_cost(r['machine_type'], preemptible=r['preemptible'])


#------------------------------------------------------------------------------
# Functions for managing methods and configuration in the repository
#------------------------------------------------------------------------------
def list_methods(namespace=None):
    """
    List all methods in the repository
    """
    r = firecloud.api.list_repository_methods()
    assert r.status_code==200
    r = r.json()

    if namespace is not None:
        r = [m for m in r if m['namespace']==namespace]

    return pd.DataFrame(r).sort_values(['name', 'snapshotId'])


def get_method(namespace, name):
    """
    Get all available versions of a method from the repository
    """
    r = firecloud.api.list_repository_methods()
    assert r.status_code==200
    r = r.json()
    r = [m for m in r if m['name']==name and m['namespace']==namespace]
    return r


def get_method_version(namespace, name):
    """
    Get latest method version
    """
    r = get_method(namespace, name)
    return np.max([m['snapshotId'] for m in r])


def list_configs(namespace=None):
    """
    List all configurations in the repository
    """
    r = firecloud.api.list_repository_configs()
    assert r.status_code==200
    r = r.json()

    if namespace is not None:
        r = [m for m in r if m['namespace']==namespace]

    return pd.DataFrame(r).sort_values(['name', 'snapshotId'])


def get_config(namespace, name):
    """
    Get all versions of a configuration from the repository
    """
    r = firecloud.api.list_repository_configs()
    assert r.status_code==200
    r = r.json()
    r = [m for m in r if m['name']==name and m['namespace']==namespace]
    return r


def get_config_version(namespace, name):
    """
    Get latest config version
    """
    r = get_config(namespace, name)
    return np.max([m['snapshotId'] for m in r])


def print_methods(namespace):
    """
    Print all methods in a namespace
    """
    r = firecloud.api.list_repository_methods()
    assert r.status_code==200
    r = r.json()
    r = [m for m in r if m['namespace']==namespace]
    methods = np.unique([m['name'] for m in r])
    for k in methods:
        print('{}: {}'.format(k, np.max([m['snapshotId'] for m in r if m['name']==k])))


def print_configs(namespace):
    """
    Print all configurations in a namespace
    """
    r = firecloud.api.list_repository_configs()
    assert r.status_code==200
    r = r.json()
    r = [m for m in r if m['namespace']==namespace]
    configs = np.unique([m['name'] for m in r])
    for k in configs:
        print('{}: {}'.format(k, np.max([m['snapshotId'] for m in r if m['name']==k])))


def get_wdl(method_namespace, method_name, snapshot_id=None):
    """
    Get WDL from repository
    """
    if snapshot_id is None:
        snapshot_id = get_method_version(namespace, name)

    r = firecloud.api.get_repository_method(method_namespace, method_name, snapshot_id)
    assert r.status_code==200
    return r.json()['payload']


def compare_wdls(mnamespace1, mname1, mnamespace2, mname2):
    """
    Compare WDLs from two methods
    """
    v1 = get_method_version(mnamespace1, mname1)
    v2 = get_method_version(mnamespace2, mname2)
    wdl1 = get_wdl(mnamespace1, mname1, v1)
    wdl2 = get_wdl(mnamespace2, mname2, v2)
    print('Comparing:')
    print('< {}:{}.v{}'.format(mnamespace1, mname1, v1))
    print('> {}:{}.v{}'.format(mnamespace2, mname2, v2))
    cmd = 'diff <(echo \''+wdl1+'\') <(echo \''+wdl2+'\')'
    d = subprocess.run(cmd, shell=True, executable='/bin/bash', stdout=subprocess.PIPE)
    print(d.stdout.decode())


def compare_wdl(mnamespace, mname, wdl_path):
    """
    Compare method WDL to file
    """
    v = get_method_version(mnamespace, mname)
    wdl1 = get_wdl(mnamespace, mname, v)
    with open(wdl_path) as f:
        wdl2 = f.read()
    print('Comparing:')
    print('< {}'.format(wdl_path))
    print('> {}:{}.v{}'.format(mnamespace, mname, v))
    cmd = 'diff <(echo \''+wdl1+'\') <(echo \''+wdl2+'\')'
    d = subprocess.run(cmd, shell=True, executable='/bin/bash', stdout=subprocess.PIPE)
    print(d.stdout.decode())

def redact_outdated_method_versions(method_namespace, method_name):
    """

    """
    r = firecloud.api.list_repository_methods()
    assert r.status_code==200
    r = r.json()
    r = [m for m in r if m['name'] == method_name and m['namespace'] == method_namespace]
    versions = np.array([m['snapshotId'] for m in r])
    print('Latest version: {}'.format(np.max(versions)))
    versions = versions[versions!=np.max(versions)]
    for i in versions:
        print('  * deleting version {}'.format(i))
        r = firecloud.api.delete_repository_method(method_namespace, method_name, i)
        assert r.status_code==200


def update_method(namespace, method, synopsis, wdl_file, public=False):
    """
    push new version, then redact previous version(s)
    """
    # check whether prior version exists
    r = get_method(namespace, method)
    old_version = None
    if r:
        old_version = np.max([m['snapshotId'] for m in r])
        print('Method {}/{} exists. SnapshotID: {}'.format(
            namespace, method, old_version))

    # push new version
    r = firecloud.api.update_repository_method(namespace, method, synopsis, wdl_file)
    assert r.status_code==201
    print("Successfully pushed {}/{}. New SnapshotID: {}".format(namespace, method, r.json()['snapshotId']))

    if public:
        print('  * setting public read access.')
        r = firecloud.api.update_repository_method_acl(namespace, method, r.json()['snapshotId'], [{'role': 'READER', 'user': 'public'}])

    # delete old version
    if old_version is not None:
        r = firecloud.api.delete_repository_method(namespace, method, old_version)
        assert r.status_code==200
        print("Successfully deleted SnapshotID {}.".format(old_version))


#------------------------------------------------------------------------------
# VM costs
#------------------------------------------------------------------------------
def get_vm_cost(machine_type, preemptible=True):
    """
    Cost per hour
    """
    preemptible_dict = {
        'n1-standard-1': 0.0100,  # 3.75 GB
        'n1-standard-2': 0.0200,  # 7.5 GB
        'n1-standard-4': 0.0400,  # 15  GB
        'n1-standard-8': 0.0800,  # 30  GB
        'n1-standard-16':0.1600,  # 60  GB
        'n1-standard-32':0.3200,  # 120 GB
        'n1-standard-32':0.6400,  # 240 GB
        'n1-highmem-2':  0.0250,  # 13  GB
        'n1-highmem-4':  0.0500,  # 26  GB
        'n1-highmem-8':  0.1000,  # 52  GB
        'n1-highmem-16': 0.2000,  # 104 GB
        'n1-highmem-32': 0.4000,  # 208 GB
        'n1-highmem-64': 0.8000   # 416 GB
    }

    standard_dict = {
        'n1-standard-1': 0.0475,
        'n1-standard-2': 0.0950,
        'n1-standard-4': 0.1900,
        'n1-standard-8': 0.3800,
        'n1-standard-16': 0.7600,
        'n1-standard-32': 1.5200,
        'n1-standard-64': 3.0400,
        'n1-highmem-2':  0.1184,
        'n1-highmem-4':  0.2368,
        'n1-highmem-8':  0.4736,
        'n1-highmem-16': 0.9472,
        'n1-highmem-32': 1.8944,
        'n1-highmem-64': 3.7888
    }

    if preemptible:
        return preemptible_dict[machine_type]
    else:
        return standard_dict[machine_type]

def main(argv=None):

    sys.exit()

    if not argv:
        argv = sys.argv

    # Initialize core parser
    descrip  = 'dalmatian [OPTIONS] CMD [arg ...]\n'
    descrip += '       dalmatian [ --help | -v | --version ]'
    parser = argparse.ArgumentParser(description='dalmatian: the loyal companion to FISS')



if __name__ == '__main__':
    sys.exit(main())
