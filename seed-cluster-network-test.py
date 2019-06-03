#!/usr/bin/env python3

import subprocess
import sys
import re
import json
import argparse
import os

#----------------------------------------------------------------------------

get_calico_pod_cmd="set -o pipefail;kubectl get pods -n kube-system -o wide | grep calico-node  | awk '{ print $1, $6, $7 }'"
get_nodes_cmd="set -o pipefail;kubectl get nodes -o jsonpath=\"{.items[*].status.addresses[:2].address}\""
exec_snippet="kubectl exec -it -n {} {} -c {} -- {}"
cp_snippet="kubectl cp {} {}/{}:{} -c {}"

get_namespaces_cmd="kubectl get ns -o jsonpath=\"{.items[*].metadata.name}\""
get_control_plane_pods="kubectl get pods -n %s -o json | jq '[.items[]|{\"name\": .metadata.name,\"hostIP\": .status.hostIP,\"podIP\": .status.podIP}]'"
#----------------------------------------------------------------------------


class Node:
    def __init__(self):
        self.can_reach = []
        self.can_not_reach = []
        self.error = None

    def __str__(this):
        can_reach_nodes = " ".join(this.can_reach)
        return (this.name + " " + this.ip + " " + this.calico_pod_name + "\n" +
                "Can reach nodes: " + can_reach_nodes)

class Pod:
    pass

class Container:
    pass

class ControlPlane:
    pass


def get_cluster_nodes():
    nodes = subprocess.run(get_nodes_cmd, shell=True, capture_output=True, check=True, encoding="utf-8")
    nodes = str(nodes.stdout).split()
    nodeMap = {}
    for i in range(0, len(nodes), 2):
        node = Node()
        node.ip = nodes[i]
        node.name = nodes[i+1]
        nodeMap[node.ip] = node

    calico_cmd = subprocess.run(get_calico_pod_cmd, shell=True, capture_output=True, check=True, encoding="utf-8")
    calico_pods = calico_cmd.stdout.split("\n")
    calico_pods = list(filter(lambda x: x != "", calico_pods))
    for calico_pod in calico_pods:
        s = calico_pod.split(" ")
        node = nodeMap[s[1]]
        node.calico_pod_name = s[0]
    return nodeMap


def copy_script_to_pod(pod):
    cp_cmd = cp_snippet.format("pingmany", "kube-system", pod, "pingmany", "calico-node")
    res = subprocess.run(cp_cmd, shell=True, capture_output=True, check=True, encoding="utf-8")

def exec(ns, pod, container, pcmd):
    cmd = exec_snippet.format(ns, pod, container, pcmd)
    res = subprocess.run(cmd, shell=True, capture_output=True, encoding="utf-8")
    if res.returncode != 0:
        sys.stderr.write("Unable to run {} on pod {}, skipping:\n".format(pcmd, pod))
        sys.stderr.write(res.stderr)
        return ""
    else:
        return res.stdout    

def rm_pingmany(ns, pod, container):
    cmd = exec_snippet.format(ns, pod, container, "rm /pingmany")
    subprocess.run(cmd, shell=True, capture_output=True, check=True, encoding="utf-8")



def parse_ping_out(node, output):

    pt = re.compile('([\d]+.[\d]+.[\d]+.[\d]+) ping statistics')
    lines = output.split("\n")
    while len(lines) > 0:
        s = lines.pop(0)
        match = re.search(pt, s)
        if match is not None:
            loss_line = lines.pop(0)
            if re.search("1 received", loss_line):
                node.can_reach.append(match.group(1))
            else:
                node.can_not_reach.append(match.group(1))

def run_ping_test(nodes):
    all_nodes = nodes.keys()
    for k in nodes:
        n = nodes[k]
        print("Running test on node " + n.ip)
        other_nodes = list(filter(lambda x: x != n.ip, all_nodes))

        try:
            copy_script_to_pod(n.calico_pod_name)
            ping_out = exec("kube-system", n.calico_pod_name, "calico-node", "/pingmany " + " ".join(other_nodes))
            parse_ping_out(n, ping_out)
            rm_pingmany("kube-system", n.calico_pod_name, "calico-node")
        except subprocess.CalledProcessError as e:        
            print(e)
            print(e.stdout)
            print(e.stderr)
            n.error = str(e)

def print_statistics(nodes):
    for node in nodes.values():
        if node.error is not None:
            print("Error running test on node " + node.ip)
            print(node.error)
        else:
            print("Node {} can reach nodes {}".format(node.ip, " ".join(node.can_reach)))
            if len(node.can_not_reach) > 0:
                print("Node {} can not reach nodes {}".format(node.ip, " ".join(node.can_not_reach)))
            if (len(nodes)-1) == len(node.can_reach):
                print("OK")
            else:
                print("not OK")

def examine_shoot_control_plane(ns):
    print("Examine control plane " + ns)
    nodes = subprocess.run(get_control_plane_pods % ns, shell=True, capture_output=True, check=True, encoding="utf-8")
    control_plane = json.loads(nodes.stdout)
    kube_apiserver_name = None
    etcd_ip = None
    for cp in control_plane:
        kube_apiserver_name = cp["name"] if cp["name"].startswith("kube-apiserver") else kube_apiserver_name
        etcd_ip = cp["podIP"] if cp["name"].startswith("etcd-main-0") else etcd_ip

    if kube_apiserver_name is not None and etcd_ip is not None:
        ping_exec = exec_snippet.format(ns, kube_apiserver_name, "kube-apiserver", "ping -W 2 -c 1 " + etcd_ip)
        print(ping_exec)
        r = subprocess.run(ping_exec, shell=True, capture_output=True, encoding="utf-8")
        print(r.stdout)


def get_pods():
    pods = subprocess.run("kubectl get pods --all-namespaces -o json", shell=True, capture_output=True, check=True, encoding="utf-8")
    pods_json = json.loads(pods.stdout)
    pods_array = []
    for it in pods_json["items"]:
        metadata = it["metadata"]
        if "namespace" in metadata and metadata["namespace"] == "kube-system":
            continue
        p = Pod()
        pods_array.append(p)
        p.name = metadata["name"]
        p.namespace = metadata["namespace"]
        status = it["status"]
        p.hostIP = status["hostIP"]
        if "podIP" in status:
            p.podIP = status["podIP"] 
        p.containers = []
        if "containerStatuses" in status:
            for cs in status["containerStatuses"]:
                if "containerID" in cs and "image" in cs:
                    c = Container()
                    c.containerID = cs["containerID"]
                    c.image = cs["image"]
                    p.containers.append(c)
    return pods_array

def get_container_id(pod, id):
    for c in pod.containers:
        if c.image.find(id) != -1:
            if c.containerID.startswith("docker://"):
                return c.containerID[9:]
    return None

def ping_etcd_from_apiserver(api_server, root_pod_map, etcd_pod):
    print("kube-apiserver {}/{} connectivity test with {}/{}".format(api_server.namespace, api_server.name, etcd_pod.namespace, etcd_pod.name))
    containerID = get_container_id(api_server, "k8s.gcr.io/hyperkube")
    root_pod = root_pod_map[api_server.hostIP]

    cmd = "kubectl exec -it {} -- chroot /root docker inspect {}".format(root_pod.name, containerID)
    print("Running " + cmd)
    inspect = subprocess.run(cmd, shell=True, capture_output=True, encoding="utf-8")
    if inspect.returncode != 0:
        print("Unable to run test from pod " + api_server.name + ": " + inspect.stderr)
        return
    inspect_json = json.loads(inspect.stdout)
    api_server_pid = inspect_json[0]["State"]["Pid"]

    cmd = "kubectl exec -it {} -- nsenter -n/proc/{}/ns/net  -- nc -vz -w 2 {} {}".format(root_pod.name, api_server_pid, etcd_pod.podIP, 2379)
    print("Running " + cmd)
    nc = subprocess.run(cmd, shell=True, capture_output=True, encoding="utf-8")
    if nc.returncode != 0:
        print("failed: " + nc.stdout + nc.stderr)
    else:
        print("success")
    
def check_etcd_from_apiservers(node_map, pods):

    etcds = filter(lambda x: x.name == "etcd-main-0", pods)
    ntpods = filter(lambda x: x.name.startswith("network-test-"), pods)
    api_servers = filter(lambda x: x.name.startswith("kube-apiserver"), pods)
    ntmap = {}
    for n in ntpods:
        ntmap[n.podIP] = n

    shoot_etcd_map = {}
    for etcd in etcds:
        shoot_etcd_map[etcd.namespace] = etcd

    for aserver in api_servers:
        ns = aserver.namespace
        target_etcd = shoot_etcd_map[ns]
        aserver.target_etcd = target_etcd
        ping_etcd_from_apiserver(aserver, ntmap, target_etcd)

def get_namespaces():
    nodes = subprocess.run(get_namespaces_cmd, shell=True, capture_output=True, check=True, encoding="utf-8")
    ns = nodes.stdout.split(" ")
    return list(filter(lambda x: x.startswith("shoot--"), ns))

def deploy_root_daemonset():
    daemonset_path = os.path.dirname(__file__)
    daemonset_file = os.path.join(daemonset_path, "privileged-daemonset.yaml")

    cmd = "kubectl apply -f " + daemonset_file
    nodes = subprocess.run(cmd, shell=True, capture_output=True, check=True, encoding="utf-8")
    # wait until running
    cmd = "kubectl get pods | grep network-test | awk '{ print $1,$3 }'"
    retries = 0
    running = False
    while not running and retries < 20:
        retries += 1
        res = subprocess.run(cmd, shell=True, capture_output=True, check=True, encoding="utf-8")
        if res.returncode == 0:
            out = res.stdout.split("\n")
            if len(out) == 0:
                # no process, not running
                continue 
            for l in out:
                n = l.split(" ")
                if n != "Running":
                    continue
            running = True

    if not running:
        raise Exception("Not all necessary network-test pods are running.")


def undeploy_root_daemonset():
    cmd = "kubectl delete daemonset network-test"
    subprocess.run(cmd, shell=True, capture_output=True, check=True, encoding="utf-8")

#----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Seed cluster connectivity test.")
    parser.add_argument("--nodes", action="store_true", help="node connectivity test")
    parser.add_argument("--control-planes", action="store_true", help="control plane components connectivity test")
    args = parser.parse_args()
    if not (args.nodes or args.control_planes):
        parser.print_help()
        sys.exit(1)

 
    try:
        deploy_root_daemonset()

        node_map = get_cluster_nodes()
        if args.nodes:
            print("Running node connectivity test.")
            run_ping_test(node_map)
            print_statistics(node_map)

        if args.control_planes:

            ns = get_namespaces()
            pods = get_pods()
            check_etcd_from_apiservers(node_map, pods)
#            for n in ns:
#                examine_shoot_control_plane(n)
    except subprocess.CalledProcessError as e:        
        print(e)
        print(e.stdout)
        print(e.stderr)
    finally:
        undeploy_root_daemonset()

if __name__ == "__main__":
    main()