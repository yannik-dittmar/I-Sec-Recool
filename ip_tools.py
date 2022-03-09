from http import server
import signal
import socket
import ipaddress
import subprocess
from telnetlib import NOP
import threading
import time
from typing import Dict, List, Set
import nmap
import os
import json
from json import JSONEncoder
from colored import fg, bg, attr, stylize
import inquirer
import recool

def parse_ip(ip):
    try:
        return str(ipaddress.ip_address(ip))
    except ValueError:
        return False

def default_ip():
    #return socket.gethostbyname(socket.gethostname())
    #return "172.22.3.170"
    return "192.168.188.10"
    #return "10.129.0.217"

def keys_exists(element, *keys):
    '''
    Check if *keys (nested) exists in `element` (dict).
    '''
    if not isinstance(element, dict):
        raise AttributeError('keys_exists() expects dict as first argument.')
    if len(keys) == 0:
        raise AttributeError('keys_exists() expects at least two arguments, one given.')

    _element = element
    for key in keys:
        try:
            _element = _element[key]
        except KeyError:
            return False
    return True

class NetworkDevice():
    done_ping_scan: bool
    done_full_scan: bool
    is_up: bool
    name: str
    ip: str
    services: Dict[str, str]

    def __init__(self, **kv):
        self.ip = ""
        self.__dict__.update(kv)

    def __getattr__(self, item):
        return None

    def add_service(self, port, info):
        if not self.services:
            self.services = {}
        self.services[port] = info

    def __str__(self) -> str:
        string = ""

        # Name and IP
        if self.name:
            string += f'==={stylize(self.name, recool.STYLE_HIGHLIGHT)}===\n'
        else:
            string += f'==={stylize(self.ip, recool.STYLE_HIGHLIGHT)}===\n'
        string += f'{stylize("IP:", recool.STYLE_HIGHLIGHT)} {self.ip}\n'

        # Services
        print_services = False
        ports = self.services.keys()
        for port in sorted(ports):
            portInfo = self.services[port]
            if portInfo["state"] == "open":
                print_services = True
                break

        if print_services:
            string += f'Open {stylize("TCP", recool.STYLE_HIGHLIGHT)}-Ports:\n'
            for port in sorted(ports):
                portInfo = self.services[port]
                if portInfo["state"] != "open":
                    continue
                
                portName = portInfo["name"]
                portProd = portInfo["product"]
                portVer = portInfo["version"]

                if portName == "":
                    string += f' - {stylize(port, recool.STYLE_HIGHLIGHT)}\n'
                elif portProd == "":
                    string += f' - {stylize(port, recool.STYLE_HIGHLIGHT)} ({portName})\n'
                elif portVer == "":
                    string += f' - {stylize(port, recool.STYLE_HIGHLIGHT)} ({portName} - {portProd})\n'
                else:
                    string += f' - {stylize(port, recool.STYLE_HIGHLIGHT)} ({portName} - {portProd}, {portVer})\n'

        return string

class NetworkEncoder(JSONEncoder):
    def default(self, o):
        if isinstance(o, set):
            return list(o)
        if isinstance(o, NetworkDevice):
            parsed = dict(o.__dict__)
            if 'ip' in parsed:
                del parsed['ip']
            return parsed

        return o.__dict__

class NmapProgressUpdater(threading.Thread):
    abort: bool
    spinner: any
    stats_path: str
    prefix: str

    def __init__(self, **kv):
        threading.Thread.__init__(self)
        self.abort = False
        self.__dict__.update(kv)

    def run(self):
        time.sleep(2)
        self.prefix = self.spinner.text
        while not self.abort:
            stats = ""
            if os.path.exists(self.stats_path):
                with open(self.stats_path, mode='r') as stats_f:
                    for line in stats_f:
                        stats = line.rstrip("\n")
                self.spinner.text = f'{self.prefix} - {stats}'
            time.sleep(1)

class NetworkScanner:
    nmap_proc: subprocess.Popen

    def __init__(self, args, spinner):
        self.nmap = nmap.PortScanner()
        self.args = args
        self.devices: Dict[str, NetworkDevice] = {}
        self.spinner = spinner
        self.nmap_proc = None
        self.interrupt_action = None

    def signal_handler(self, sig, frame):
        questions = [
            inquirer.List('action',
                            message="What do you want to do?",
                            carousel=True,
                            choices=['Continue scanning', 'Skip this scan', 'Skip this scan and never scan again', 'Exit recool'],
                        ),
        ]
        with self.spinner.hidden():
            answers = inquirer.prompt(questions)
        if self.nmap_proc and answers['action'] != 'Continue scanning':
            self.nmap_proc.send_signal(signal.SIGINT)
        if answers['action'] == 'Exit recool':
            exit(0)

    def scan(self, hosts: List[str], args: List[str]):
        original_sigint_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, self.signal_handler)

        # Start nmap scan
        thread = NmapProgressUpdater(spinner=self.spinner, stats_path=f'{self.args.storage}/nmap.log')
        thread.daemon = True
        thread.start()
        #os.popen(f'nmap -oX {self.args.storage}/scan.xml --stats-every 5s {args} {self.args.speed} {" ".join(hosts)} > {self.args.storage}/nmap.output').read()
        with open(f'{self.args.storage}/nmap.log', 'w') as log, open(f'{self.args.storage}/nmap.error', 'w') as err:
            try:
                self.nmap_proc = subprocess.Popen(['nmap', '-oX', f'{self.args.storage}/scan.xml', '--stats-every', '5s', *args, self.args.speed, *hosts], stdout=log, stderr=err, start_new_session=True)
                self.nmap_proc.wait()
            finally:
                self.nmap_proc.kill()
                self.nmap_proc = None
        thread.abort = True
        signal.signal(signal.SIGINT, original_sigint_handler)

        # Parse scan results
        try:
            with open(f'{self.args.storage}/scan.xml',mode='r') as scan_file:
                result = self.nmap.analyse_nmap_xml_scan(nmap_xml_output=scan_file.read())
        except (nmap.PortScannerError, FileNotFoundError):
            return None

        return result["scan"]

    def update_model(self):
        self.spinner.text = f'Updating the nplan model...'
        os.popen(f'{self.args.nplan} -nmap {self.args.storage}/scan.xml -json {self.args.storage}/model.json > /dev/null').read()
        os.popen(f'{self.args.nplan} -export -json {self.args.storage}/model.json -drawio {self.args.storage}/drawio.xml > /dev/null').read()

        self.spinner.text = f'Saving the current state... (DO NOT EXIT)'
        with open(f'{self.args.storage}/recool_save_new.json', 'w') as outfile:
            json.dump(self.devices, outfile, cls=NetworkEncoder)
        if os.path.exists(f'{self.args.storage}/recool_save.json'):
            os.remove(f'{self.args.storage}/recool_save.json')
        os.rename(f'{self.args.storage}/recool_save_new.json', f'{self.args.storage}/recool_save.json')

    def load_devices(self):
        if not os.path.exists(f'{self.args.storage}/recool_save.json'):
            return
        
        storage = {}
        with open(f'{self.args.storage}/recool_save.json', 'r') as f:
            storage = json.load(f)

        for ip, device in storage.items():
            self.devices[ip] = NetworkDevice(**device)
            self.devices[ip].ip = ip
    
    def find_by_ip(self, ip, create=True):
        if keys_exists(self.devices, ip):
            return self.devices[ip]

        if create:
            device = NetworkDevice(ip=ip)
            self.devices[ip] = device
            return device
        return None

    def parse_device_data(self, ip, data):
        device: NetworkDevice = self.find_by_ip(ip)

        # Hostname
        if (keys_exists(data, 'hostnames', 0, 'name') and 
                data['hostnames'][0]['name']):
            device.name = data['hostnames'][0]['name']
        
        if (keys_exists(data, 'tcp')):
            for port, info in data['tcp'].items():
                device.add_service(port, info)

        return device

    def ping_scan_subnet(self, subnet: str):
        iface = ipaddress.ip_interface(self.args.ip + '/' + subnet)
        self.spinner.text = f'Performing ping-scan on subnet {stylize(str(iface.network), recool.STYLE_HIGHLIGHT)}'
        
        # Collect hosts
        hosts = []
        for host in iface.network.hosts():
            device: NetworkDevice = self.find_by_ip(str(host))
            if not device.done_ping_scan and not device.is_up:
                hosts.append(str(host))
        
        if not hosts:
            return

        # Perform scan and collect data
        #result = self.scan(hosts, '-sn -n')
        result = self.scan(hosts, ['-sn', '-n'])
        for ip, data in result.items():
            device = self.parse_device_data(ip, data)
            device.is_up = True
        
        # Update done_ping_scan
        for host in iface.network.hosts():
            device = self.find_by_ip(str(host))
            device.done_ping_scan = True

        self.update_model()

        #self.spinner.write(json.dumps(self.devices, cls=NetworkEncoder))

    def full_scan_up(self, devices=None):
        if not devices:
            devices = self.devices

        for ip, device in devices.items():
            if not device.is_up or device.done_full_scan:
                continue
        
            self.spinner.text = f'Performing full-scan for: {stylize(device.ip, recool.STYLE_HIGHLIGHT)}'
            result = self.scan([device.ip], ['-A', '-p-', '-sV'])
            for ip, data in result.items():
                device = self.parse_device_data(ip, data)
                device.done_full_scan = True
                self.spinner.write(str(device))

            self.update_model()

            #self.spinner.write(json.dumps(self.devices, cls=NetworkEncoder))

    def test(self):
        result = self.scan(["192.168.188.30"], '-A -p- -sV')
        #result = self.nmap.scan(hosts="10.129.0.2", arguments=f'{self.args.speed}')["scan"]
        #self.spinner.write(self.nmap.)