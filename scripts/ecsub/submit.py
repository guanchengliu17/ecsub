# -*- coding: utf-8 -*-
"""
Created on Wed Mar 14 13:06:19 2018

@author: Okada
"""

import boto3
import os
import shutil
import multiprocessing
import string
import random
import datetime
import time
import json
import ecsub.aws
import ecsub.aws_config
import ecsub.tools
import ecsub.metrics

def read_tasksfile(tasks_file, cluster_name):
    
    tasks = []
    header = []

    for line in open(tasks_file).readlines():
        if header == []:
            for item in line.rstrip("\r\n").split("\t"):
                v = item.strip(" ").split(" ")
                if v[0] == "":
                    header.append({"type": "", "recursive": False, "name": ""})
                
                elif v[0].lower() == "--env":
                    header.append({"type": "env", "recursive": False, "name": v[-1]})
                elif v[0].lower() == "--input-recursive":
                    header.append({"type": "input", "recursive": True, "name": v[-1]})
                elif v[0].lower() == "--input":
                    header.append({"type": "input", "recursive": False, "name": v[-1]})
                elif v[0].lower() == "--output-recursive":
                    header.append({"type": "output", "recursive": True, "name": v[-1]})
                elif v[0].lower() == "--output":
                    header.append({"type": "output", "recursive": False, "name": v[-1]})
                else:
                    print (ecsub.tools.error_message (cluster_name, None, "type %s is not support." % (v[0])))
                    return None
            continue
        
        tasks.append(line.rstrip("\r\n").split("\t"))

    return {"tasks": tasks, "header": header}


def write_runsh(task_params, runsh, shell):
   
    run_template = """set -ex
pwd

SCRIPT_ENVM_NAME=`basename ${{SCRIPT_ENVM_PATH}}`
SCRIPT_EXEC_NAME=`basename ${{SCRIPT_EXEC_PATH}}`

aws s3 cp ${{SCRIPT_ENVM_PATH}} ${{SCRIPT_ENVM_NAME}} --only-show-errors
aws s3 cp ${{SCRIPT_EXEC_PATH}} ${{SCRIPT_EXEC_NAME}} --only-show-errors

source ${{SCRIPT_ENVM_NAME}}
df -h

{download_script}

# exec
{shell} ${{SCRIPT_EXEC_NAME}}

#if [ $? -gt 0 ]; then exit $?; fi

# upload
{upload_script}
"""

    dw_text = ""
    up_text = ""    
    for i in range(len(task_params["header"])):

        if task_params["header"][i]["type"] == "input":
            cmd_template = 'if test -n "${name}"; then aws s3 cp --only-show-errors {r_option} $S3_{name} ${name}; fi\n'
            r_option = ""
            if task_params["header"][i]["recursive"]:
                r_option = "--recursive"
            dw_text += cmd_template.format(
                r_option = r_option,
                name = task_params["header"][i]["name"])
            
        elif task_params["header"][i]["type"] == "output":
            cmd_template = 'if test -n "${name}"; then aws s3 cp --only-show-errors {r_option} ${name} $S3_{name}; fi\n'
            r_option = ""
            if task_params["header"][i]["recursive"]:
                r_option = "--recursive"
            up_text += cmd_template.format(
                r_option = r_option,
                name = task_params["header"][i]["name"])
            
    open(runsh, "w").write(run_template.format(
        shell = shell,
        download_script = dw_text,
        upload_script = up_text
    ))
    
def write_setenv(task_params, setenv, no):
   
    f = open(setenv, "w")
    
    for i in range(len(task_params["tasks"][no])):
        
        if task_params["header"][i]["type"] == "input":
            f.write('export S3_%s="%s"\n' % (task_params["header"][i]["name"], task_params["tasks"][no][i]))
            f.write('export %s="%s"\n' % (task_params["header"][i]["name"], task_params["tasks"][no][i].replace("s3://", "/scratch/AWS_DATA/")))
        elif task_params["header"][i]["type"] == "output":
            f.write('export S3_%s="%s"\n' % (task_params["header"][i]["name"], task_params["tasks"][no][i]))
            f.write('export %s="%s"\n' % (task_params["header"][i]["name"], task_params["tasks"][no][i].replace("s3://", "/scratch/AWS_DATA/")))
        elif task_params["header"][i]["type"] == "env":
            f.write('export %s="%s"\n' % (task_params["header"][i]["name"], task_params["tasks"][no][i]))
            
    f.close()

def check_inputfiles2(task_params, cluster_name):

    files = []
    dirs = []
    for task in task_params["tasks"]:
        for i in range(len(task)):
            if task_params["header"][i]["type"] != "input":
                continue
            
            path = task[i].replace("s3://", "", 1).strip("/").rstrip("/")
            if path == "":
                continue
            
            if task_params["header"][i]["recursive"]:
                dirs.append(path)
            else:
                files.append(path)
    
    file_list = sorted(list(set(files)))
    dir_list = sorted(list(set(dirs)))
    
    uncheck_dirs = []
    uncheck_dirs.extend(dir_list)
    for d in dir_list:
        for f in file_list:
            if f.startswith(d):
                uncheck_dirs.remove(d)
                break

    tree = {}
    for path in file_list:
        bucket = path.split("/")[0]
        if not bucket in tree:
            tree[bucket] = {}
            tree[bucket]["files"] = []
            tree[bucket]["dirs"] = []
        tree[bucket]["files"].append(path.replace(bucket + "/", "", 1))

    for path in uncheck_dirs:
        bucket = path.split("/")[0]
        if not bucket in tree:
            tree[bucket] = {}
            tree[bucket]["files"] = []
            tree[bucket]["dirs"] = []
        tree[bucket]["dirs"].append(path.replace(bucket + "/", "", 1))
    
    s3 = boto3.resource('s3')
    for key in tree:
        bucket = s3.Bucket(key)
        print (ecsub.tools.info_message (cluster_name, None, "checking s3 bucket '%s'..." % (key)))
        for obj in bucket.objects.all():
            if obj.key in tree[key]["files"]:
                tree[key]["files"].remove(obj.key)

            match = [s for s in tree[key]["dirs"] if obj.key.startswith(s)]
            for d in match:
                tree[key]["dirs"].remove(d)
            if len(tree[key]["files"]) == 0 and len(tree[key]["dirs"]) == 0:
                break
            
    result = []
    for key in tree:
        for typ in tree[key]:
            for path in tree[key][typ]:
                result.append("%s/%s" % (key, path))
    
    return result
    
def check_inputfiles(task_params, aws_instance, no):
    
    task = task_params["tasks"][no]
    for i in range(len(task)):
        if task_params["header"][i]["type"] != "input":
            continue
        
        path = task[i].rstrip("/")
        if path == "":
            continue
        
        if not aws_instance.check_file(path, no):
            return False

    return True
    
def upload_scripts(task_params, aws_instance, local_root, s3_root, script, cluster_name, shell):

    runsh = local_root + "/run.sh"
    s3_runsh = s3_root + "/run.sh"
    write_runsh(task_params, runsh, shell)

    s3_setenv_list = []
    for i in range(len(task_params["tasks"])):
        setenv = local_root + "/setenv.%d.sh" % (i)
        s3_setenv = s3_root + "/setenv.%d.sh" % (i)
        write_setenv(task_params, setenv, i)
        s3_setenv_list.append(s3_setenv)
        
    aws_instance.s3_copy(local_root, s3_root, True)
    
    s3_script = s3_root + "/" + os.path.basename(script)
    aws_instance.s3_copy(script, s3_script, False)
    
    aws_instance.set_s3files(s3_runsh, s3_script, s3_setenv_list)
    
    return True

def _run_task(aws_instance, no, instance_id):
    
    system_error = False
    exit_code = 1
    task_log = None
    
    try:
        (exit_code, task_log) = aws_instance.run_task(no, instance_id)
        if exit_code == 127:
            system_error = True
    
    except Exception as e:
        print (ecsub.tools.error_message (aws_instance.cluster_name, no, e))
    
    aws_instance.terminate_instances(instance_id, no)
    
    return (exit_code, task_log, system_error)

def submit_task_ondemand(aws_instance, no):
    
    exit_code = 1
    task_log = None
    
    if not aws_instance.set_ondemand_price(no):
        return (exit_code, task_log)
    
    for i in range(3):
        instance_id = aws_instance.run_instances_ondemand (no)
        if instance_id == None:
            break
        
        (exit_code, task_log, system_error) = _run_task(aws_instance, no, instance_id)
            
        if system_error:
            continue
        else:
            return (exit_code, task_log)
        
    return (exit_code, task_log)

def submit_task_spot(aws_instance, no):

    exit_code = 1
    task_log = None
    
    for itype in aws_instance.aws_ec2_instance_type_list:
        aws_instance.task_param[no]["aws_ec2_instance_type"] = itype
        if not aws_instance.set_ondemand_price(no):
            continue
        if not aws_instance.set_spot_price(no):
            continue
        
        for i in range(3):
            instance_id = aws_instance.run_instances_spot (no)
            if instance_id == None:
                break

            (exit_code, task_log, system_error) = _run_task(aws_instance, no, instance_id)
            aws_instance.cancel_spot_instance_requests (no = no, instance_id = instance_id)
                
            if system_error:
                continue
            else:
                return (exit_code, task_log, False)
    
    return (exit_code, task_log, True)

def _hour_delta(start_t, end_t):
    return (end_t - start_t).total_seconds()/3600.0

def _set_job_info(task_param, start_t, end_t, task_log, exit_code):
    
    info = {
        "Ec2InstanceType": task_param["aws_ec2_instance_type"],
        "End": end_t,
        "ExitCode": exit_code,
        "LogLocal": task_log, 
        "OdPrice": task_param["od_price"],
        "Start": start_t,
        "Spot": task_param["spot"],
        "SpotAz": task_param["spot_az"],
        "SpotPrice": task_param["spot_price"],
        "WorkHours": _hour_delta(start_t, end_t),
        "InstanceId": "",
        "SubnetId": "",
        "Memory": 0,
        "vCpu": 0,
    }
    
    if task_log == None:
        return info
    
    task = json.load(open(task_log))["tasks"][0]
    info["InstanceId"] = task["instance_id"]
    info["SubnetId"] = task["subnet_id"]
    info["Memory"] = task["overrides"]["containerOverrides"][0]["memory"]
    info["vCpu"] = task["overrides"]["containerOverrides"][0]["cpu"]

    return info

def _save_summary_file(job_summary, print_cost):
    
    template = " + instance-type %s (%s) %.3f USD (%s: %.3f USD), running-time %.3f Hour"
    costs = 0.0
    items = []
    for job in job_summary["Jobs"]:
        wtime = _hour_delta(job["Start"], job["End"])
        
        if job["Spot"]:
            costs += job["SpotPrice"] * wtime
            items.append(template % (job["Ec2InstanceType"], "spot", job["SpotPrice"], "od", job["OdPrice"], wtime))
        else:
            costs += job["OdPrice"] * wtime
            items.append(template % (job["Ec2InstanceType"], "ondemand", job["OdPrice"], "spot", job["SpotPrice"], wtime))            
        
        job["Start"] = ecsub.tools.datetime_to_standardformat(job["Start"])
        job["End"] = ecsub.tools.datetime_to_standardformat(job["End"])

    if print_cost:        
        message = "The cost of this job is %.3f USD. \n%s" % (costs, "\n".join(items))
        print (ecsub.tools.info_message (job_summary["ClusterName"], job_summary["No"], message))
    
    log_file = "%s/log/summary.%03d.log" % (job_summary["Wdir"], job_summary["No"]) 
    json.dump(job_summary, open(log_file, "w"), indent=4, separators=(',', ': '), sort_keys=True)
    
def submit_task(aws_instance, no, task_params, spot):
    
    job_summary = {
        "AccountId": aws_instance.aws_accountid,
        "AmiId": aws_instance.aws_ami_id,
        "AutoKey": aws_instance.aws_key_auto,
        "ClusterName": aws_instance.cluster_name,
        "ClusterArn": aws_instance.cluster_arn,
        "Ec2InstanceDiskSize": aws_instance.aws_ec2_instance_disk_size,
        "End": None,
        "Image": aws_instance.image,
        "KeyName": aws_instance.aws_key_name,
        "LogGroupName": aws_instance.log_group_name,
        "No": no,
        "Region": aws_instance.aws_region,
        "S3RunSh": aws_instance.s3_runsh,
        "S3Script": aws_instance.s3_script,
        "S3Setenv": aws_instance.s3_setenv[no],
        "SecurityGroupId": aws_instance.aws_security_group_id,
        "Shell": aws_instance.shell,
        "Spot": aws_instance.spot,
        "Start": ecsub.tools.datetime_to_standardformat(datetime.datetime.now()),
        "TaskDefinitionAn": aws_instance.task_definition_arn,
        "UseAmazonEcr": aws_instance.use_amazon_ecr,
        "Wdir": aws_instance.wdir,
        "Jobs":[]
    }
    _save_summary_file(job_summary, False)
    
    try:
        #if check_inputfiles(task_params, aws_instance, no):
        if True:
            if spot:
                start_t = datetime.datetime.now()
                (exit_code, task_log, retry) = submit_task_spot(aws_instance, no)
                job_summary["Jobs"].append(_set_job_info(
                    aws_instance.task_param[no], start_t, datetime.datetime.now(), task_log, exit_code
                ))
                
                if aws_instance.retry_od and retry:
                    start_t = datetime.datetime.now()
                    aws_instance.task_param[no]["aws_ec2_instance_type"] = aws_instance.aws_ec2_instance_type_list[0]
                    (exit_code, task_log) = submit_task_ondemand(aws_instance, no)
                    job_summary["Jobs"].append(_set_job_info(
                        aws_instance.task_param[no], start_t, datetime.datetime.now(), task_log, exit_code
                    ))
            else:
                start_t = datetime.datetime.now()
                (exit_code, task_log) = submit_task_ondemand(aws_instance, no)
                job_summary["Jobs"].append(_set_job_info(
                    aws_instance.task_param[no], start_t, datetime.datetime.now(), task_log, exit_code
                ))
            
            job_summary["SubnetId"] = aws_instance.aws_subnet_id
            job_summary["End"] = ecsub.tools.datetime_to_standardformat(datetime.datetime.now())
            ecsub.metrics.entry_point(aws_instance.wdir, no)
        else:
            exit_code = 1
            job_summary["End"] = ecsub.tools.datetime_to_standardformat(datetime.datetime.now())
        
        _save_summary_file(job_summary, True)
        return exit_code

    except KeyboardInterrupt:
        pass
    return 1

def main(params):
    
    # set cluster_name
    params["cluster_name"] = params["task_name"]
    if params["cluster_name"] == "":
        params["cluster_name"] = os.path.splitext(os.path.basename(params["tasks"]))[0] \
            + '-' \
            + ''.join([random.choice(string.ascii_letters + string.digits) for i in range(5)])
            
    # check param
    if params["aws_ec2_instance_type"] != "":
        pass
            
    elif len(params["aws_ec2_instance_type_list"]) > 0:
        if not params["spot"]:
            print (ecsub.tools.error_message (params["cluster_name"], None, "--aws-ec2-instance-type-list option is not support with ondemand-instance mode."))
            return 1
        
    else:
        print (ecsub.tools.error_message (params["cluster_name"], None, "One of --aws-ec2-instance-type option and --aws-ec2-instance-type-list option is required."))
        return 1
    
    # read tasks file
    task_params = read_tasksfile(params["tasks"], params["cluster_name"])
    if task_params == None:
        return 1
    
    if task_params["tasks"] == []:
        return 0
    
    subdir = params["cluster_name"]
    
    params["wdir"] = params["wdir"].rstrip("/") + "/" + subdir
    params["aws_s3_bucket"] = params["aws_s3_bucket"].rstrip("/") + "/" + subdir
    
    if os.path.exists (params["wdir"]):
        shutil.rmtree(params["wdir"])
        print (ecsub.tools.info_message (params["cluster_name"], None, "'%s' existing directory was deleted." % (params["wdir"])))
        
    os.makedirs(params["wdir"])
    os.makedirs(params["wdir"] + "/log")
    os.makedirs(params["wdir"] + "/conf")
    os.makedirs(params["wdir"] + "/script")

    aws_instance = ecsub.aws.Aws_ecsub_control(params, len(task_params["tasks"]))
    
    # check task-param
    if not aws_instance.check_awsconfigure():
        return 1

    # check s3-files path
    result = check_inputfiles2(task_params, params["cluster_name"])
    for r in result:
        print (ecsub.tools.error_message (params["cluster_name"], None, "input '%s' is not exist." % (r)))
    if len(result)> 0:
        return 1
    
    # write task-scripts, and upload to S3
    local_script_dir = params["wdir"] + "/script"
    s3_script_dir = params["aws_s3_bucket"].rstrip("/") + "/script"
    upload_scripts(task_params, 
                   aws_instance, 
                   local_script_dir, 
                   s3_script_dir,
                   params["script"],
                   params["cluster_name"],
                   params["shell"])

    pool = None
    try:
        # create-cluster
        # and register-task-definition
        if aws_instance.create_cluster() and aws_instance.register_task_definition():
            
            # run instance and submit task
            async_result = []
            pool = multiprocessing.Pool(processes = params["processes"])

            for i in range(len(task_params["tasks"])):
                async_result.append(pool.apply_async(submit_task, args=(aws_instance, i, task_params, params["spot"])))
                time.sleep(5)
            pool.close() 
            pool.join()
            
            aws_instance.clean_up()
            
            # success ?
            for result in async_result:
                if result.get() != 0:
                    return 1
            return 0
        
        else:
            aws_instance.clean_up()
        
    except Exception as e:
        print (ecsub.tools.error_message (params["cluster_name"], None, e))
        if pool != None:
            pool.terminate()
        aws_instance.clean_up()
        
    except KeyboardInterrupt:
        if pool != None:
            pool.terminate()
        aws_instance.clean_up()
    
    return 1
    
def entry_point(args, unknown_args):
    
    params = {
        "wdir": args.wdir,
        "image": args.image,
        "shell": args.shell,
        "use_amazon_ecr": args.use_amazon_ecr,
        "script": args.script,
        "tasks": args.tasks,
        "task_name": args.task_name,
        "aws_ec2_instance_type": args.aws_ec2_instance_type,
        "aws_ec2_instance_type_list": args.aws_ec2_instance_type_list.replace(" ", "").split(","),
        "aws_ec2_instance_disk_size": args.disk_size,
        "aws_s3_bucket": args.aws_s3_bucket,
        "aws_security_group_id": args.aws_security_group_id,
        "aws_key_name": args.aws_key_name,
        "aws_subnet_id": args.aws_subnet_id,
        "spot": args.spot,
        "retry_od": args.retry_od,
        "set_cmd": "set -x",
        "processes": args.processes,
    }
    return main(params)
    
if __name__ == "__main__":
    pass
