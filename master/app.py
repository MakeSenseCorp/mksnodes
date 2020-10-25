#!/usr/bin/python
import os
import sys
import signal
import json
import time
import thread
import threading
import re
import zipfile

from mksdk import MkSGlobals
from mksdk import MkSFile
from mksdk import MkSMasterNode
from mksdk import MkSShellExecutor
from mksdk import MkSExternalProcess
from mksdk import MkSUtils
from mksdk import MkSQueue
from mksdk import MkSFileUploader
from mksdk import MkSPackageInstaller
from mksdk import MkSScheduling

class Context():
	def __init__(self, node):
		self.ClassName 						= "Master Application"
		self.Timer 							= MkSScheduling.TimeSchedulerThreadless()
		self.File 							= MkSFile.File()
		self.Installer						= None
		self.Uploader 						= None
		self.Node							= node
		self.SystemLoaded					= False
		self.RequestHandlers				= {
			'on_node_change':				self.Request_OnNodeChangeHandler,
			'get_connections_list':			self.Request_GetConnectionsListRequestHandler,
			'get_master_public_info':		self.Request_GetMasterPublicInfoHandler,
			'get_installed_nodes_list':		self.Request_GetInstalledNodesListRequestHandler,
			'set_installed_node_info':		self.Request_SetInstalledNodeInfoRequestHandler,
			'get_services_info': 			self.Request_GetServicesInfoHandler,
			'set_service_info': 			self.Request_SetServiceInfoHandler,
			'reboot':						self.Request_RebootHandler,
			'shutdown':						self.Request_ShutdownHandler,
			'install':						self.Request_InstallHandler,
			'uninstall':					self.Request_UninstallHandler,
			'upload_file':					self.Request_UploadFileHandler,
			'get_git_packages':				self.Request_GetGitPackagesHandler,
			'undefined':					self.UndefindHandler
		}
		self.ResponseHandlers				= {
			'get_online_devices':			self.Response_GetOnlineDevicesHandler,
		}
		self.InstalledNodesDB				= None
		self.ServicesDB 					= None
		self.RunningServices				= []
		self.RunningNodes					= []
		self.NetworkDevicesList 			= []
		self.Node.DebugMode 				= True
		self.Shutdown 						= False
		self.UploadLocker					= threading.Lock()

		self.Timer.AddTimeItem(5, self.PrintConnections)

	def UndefindHandler(self, packet):
		self.Node.LogMSG("UndefindHandler",5)
		return THIS.Node.Network.BasicProtocol.BuildResponse(packet, {
			'error': 'none'
		})
	
	def Request_GetGitPackagesHandler(self, sock, packet):
		self.Node.LogMSG("({classname})# [Request_GetGitPackagesHandler]".format(classname=self.ClassName),5)

		packages = []
		nodes = self.InstalledNodesDB["installed_nodes"]
		folders = self.File.ListFoldersInPath(os.path.join(self.Node.MKSPath,"nodes"))

		for folder in folders:
			configFileStr = self.File.Load(os.path.join(self.Node.MKSPath,"nodes",folder,"system.json"))
			if (configFileStr is not None and len(configFileStr) > 0):
				configFile = json.loads(configFileStr)
				if ("info" in configFile["node"]):
					installed = 0
					if configFile["node"]["info"]["type"] not in [1,2]:
						if configFile["node"]["info"]["is_service"] == "False":
							for node in nodes:
								if configFile["node"]["info"]["uuid"] == node["uuid"]:
									installed = 1
							packages.append({
								"type": configFile["node"]["info"]["type"],
								"uuid": configFile["node"]["info"]["uuid"],
								"installed": installed
							})

		return THIS.Node.Network.BasicProtocol.BuildResponse(packet, {
			'status': 'ok',
			'packages': packages
		})
	
	def Request_UploadFileHandler(self, sock, packet):
		self.UploadLocker.acquire()
		payload = THIS.Node.Network.BasicProtocol.GetPayloadFromJson(packet)
		self.Node.LogMSG("({classname})# [Request_UploadFileHandler] {0}".format(payload["upload"]["chunk"], classname=self.ClassName),5)

		if payload["upload"]["chunk"] == 1:
			self.Uploader.AddNewUploader(payload["upload"])
		else:
			self.Uploader.UpdateUploader(payload["upload"])
		
		self.UploadLocker.release()
		return THIS.Node.Network.BasicProtocol.BuildResponse(packet, {
			'status': 'accept',
			'chunk': payload["upload"]["chunk"],
			'file': payload["upload"]["file"]
		})
	
	def Request_InstallHandler(self, sock, packet):
		payload = THIS.Node.Network.BasicProtocol.GetPayloadFromJson(packet)
		self.Node.LogMSG("({classname})# [Request_InstallHandler] {0}".format(payload,classname=self.ClassName),5)
		time.sleep(1)

		installType = payload["install"]["type"]
		if ("file" in installType):
			self.Installer.AddWorkItem({
				"method": "install_zip",
				"data": {
					"file": payload["install"]["file"]
				}
			})
		elif ("git" in installType):
			self.Installer.AddWorkItem({
				"method": "install_git",
				"data": {
					"file": payload["install"]["file"]
				}
			})
		
		return THIS.Node.Network.BasicProtocol.BuildResponse(packet, {
			'status': 'accepted',
			'file': payload["install"]["file"]
		})
	
	def Request_UninstallHandler(self, sock, packet):
		payload = THIS.Node.Network.BasicProtocol.GetPayloadFromJson(packet)
		self.Node.LogMSG("({classname})# [Request_UninstallHandler]".format(classname=self.ClassName),5)

		self.Installer.AddWorkItem({
			"method": "uninstall",
			"data": {
				"uuid": payload["uninstall"]["uuid"]
			}
		})

		return THIS.Node.Network.BasicProtocol.BuildResponse(packet, {
			'status': 'accepted',
			'uuid': payload["uninstall"]["uuid"]
		})

	def Request_RebootHandler(self, sock, packet):
		self.Node.LogMSG("({classname})# [Request_RebootHandler]".format(classname=self.ClassName),5)
		connections = THIS.Node.GetConnectedNodes()
		for key in connections:
			node = connections[key]
			if node.Obj["type"] == 2:
				self.Node.LogMSG("({classname})# [Request_RebootHandler] REBOOT".format(classname=self.ClassName),5)
				# Send reboot request to defender
				message = THIS.Node.BasicProtocol.BuildRequest("DIRECT", node.Obj["uuid"], THIS.Node.UUID, "reboot", {}, {})
				local_packet  = THIS.Node.BasicProtocol.AppendMagic(message)
				THIS.Node.SocketServer.Send(node.Socket, local_packet)
				# Return message to requestor
				payload = { 'status': 'OK' }
				return THIS.Node.BasicProtocol.BuildResponse(packet, payload)
		payload = { 'status': 'FAILD' }
		return THIS.Node.BasicProtocol.BuildResponse(packet, payload)
	
	def Request_ShutdownHandler(self, sock, packet):
		self.Node.LogMSG("({classname})# [Request_ShutdownHandler]".format(classname=self.ClassName),5)
		src = THIS.Node.Network.BasicProtocol.GetSourceFromJson(packet)
		if src == "00000000-0000-0000-0000-000000000001":
			self.ShutdownProcess()
			self.Node.Exit("Request_ShutdownHandler")

	''' 
		Description: 	Event from registered node or service.
		Return: 		N/A
	'''	
	def Request_OnNodeChangeHandler(self, sock, packet):
		self.Node.LogMSG("({classname})# Node change event recieved ...".format(classname=self.ClassName),5)
		payload = THIS.Node.Network.BasicProtocol.GetPayloadFromJson(packet)
		src = THIS.Node.Network.BasicProtocol.GetSourceFromJson(packet)

		# if src in THIS.Node.Services[103]["uuid"]:
		if ('online_devices' in payload["event"]):
			self.NetworkDevicesList = payload["online_devices"]

		return THIS.Node.Network.BasicProtocol.BuildResponse(packet, {
			'error': 'none'
		})
	
	'''
	TODO - I think this is not being used.
	'''
	def Response_GetOnlineDevicesHandler(self, sock, packet):
		self.Node.LogMSG("({classname})# Online network device list ...".format(classname=self.ClassName),5)
		payload = THIS.Node.Network.BasicProtocol.GetPayloadFromJson(packet)
		self.Node.LogMSG(payload,5)
	
	def Request_GetConnectionsListRequestHandler(self, sock, packet):
		if THIS.Node.Network.GetNetworkState() is "CONN":
			conns = []
			connections = THIS.Node.GetConnectedNodes()
			for key in connections:
				node = connections[key]
				conns.append({
					'local_type':	node.Obj["local_type"],
					'uuid':			node.Obj["uuid"],
					'ip':			node.IP,
					'port':			node.Obj["listener_port"],
					'type':			node.Obj["type"]
				})
			payload = {
				'connections': conns
			}

			return THIS.Node.Network.BasicProtocol.BuildResponse(packet, payload)

	def Request_GetInstalledNodesListRequestHandler(self, sock, packet):
		if self.InstalledNodesDB is None:
			installed = []
		else:
			installed = self.InstalledNodesDB["installed_nodes"]
		payload = {
			'installed_nodes': installed,
		}

		return THIS.Node.Network.BasicProtocol.BuildResponse(packet, payload)
	
	def Request_SetInstalledNodeInfoRequestHandler(self, sock, packet):
		self.Node.LogMSG("({classname})# [Request_SetInstalledNodeInfoRequestHandler] {0}".format(packet,classname=self.ClassName),5)
		payload = THIS.Node.Network.BasicProtocol.GetPayloadFromJson(packet)
		uuid 	= payload["uuid"]
		enabled = payload["enabled"]

		installed = self.InstalledNodesDB["installed_nodes"]
		for item in installed:
			if (item["uuid"] == uuid):
				item["enabled"] = enabled
				break
		
		self.InstalledNodesDB["installed_nodes"] = installed
		# Save new switch to database
		self.File.SaveJSON(os.path.join(self.Node.MKSPath,"nodes.json"), self.InstalledNodesDB)
		
		payload = { 'error': 'ok' }
		return THIS.Node.Network.BasicProtocol.BuildResponse(packet, payload)
	
	def Request_GetMasterPublicInfoHandler(self, sock, packet):
		# Read
		# 	Temperature						cat /sys/class/thermal/thermal_zone0/temp
		#	CPU/RAM Usage, 10 Tasks List	top -n 1
		#									ps -eo pcpu,pid,user,args | sort -k 1 -r | head -10
		#
		self.Node.LogMSG("({classname})# [Request_GetMasterPublicInfoHandler]".format(classname=self.ClassName),5)
		cpuUsage 		= 0
		temperature 	= 0
		ramTotal 		= 0
		ramUsed 		= 0
		hdTotal 		= 0
		hdUsed 			= 0
		hdAvailable 	= 0
		osType 			= ""
		boardType 		= THIS.Node.BoardType
		cpuType			= ""
		shell = MkSShellExecutor.ShellExecutor()
		
		# Get CPU usage (TODO - Not returning correct CPU values use this "top -b -d 1 -n 1")
		data = shell.ExecuteCommand("ps -eo pcpu,pid | sort -k 1 -r | head -20")
		data = re.sub(' +', ' ', data)
		cmdRows = data.split("\n")
		for row in cmdRows[1:-1]:
			cols = row.split(" ")
			if (cols[0] != ""):
				cpuUsage += float(cols[0])
			else:
				cpuUsage += float(cols[1])
		
		# Get CPU temperature
		data = shell.ExecuteCommand("cat /sys/class/thermal/thermal_zone0/temp")
		try:
			temperature = float(float(data[:-3]) / 10.0)
		except Exception as e:
			pass 
		
		# Get RAM free space
		data = shell.ExecuteCommand("free")
		data = re.sub(' +', ' ', data)
		cmdRows = data.split("\n")
		col = cmdRows[1].split(" ")
		ramTotal = int(col[1]) / 1023
		ramUsed  = int(col[2]) / 1023
		ramAvailable = ramTotal - ramUsed
		
		# Get CPU usage
		data = shell.ExecuteCommand("df")
		data = re.sub(' +', ' ', data)
		cmdRows = data.split("\n")
		for row in cmdRows[1:-1]:
			cols = row.split(" ")
			if (cols[5] == "/"):
				hdTotal 		= int(cols[1]) / (1023 * 1023)
				hdUsed 			= int(cols[2]) / (1023 * 1023)
				hdAvailable 	= int(cols[3]) / (1023 * 1023)
				break
		
		# Get OS info
		data = shell.ExecuteCommand("uname -a")
		data = re.sub(' +', ' ', data)
		col = data.split(" ")
		osType 		= col[0]
		machineName = col[1]
		cpuType		= col[11]
		
		# Get network data
		interfaces = []
		self.Utilities = MkSUtils.Utils()
		items = self.Utilities.GetSystemIPs()
		for item in items:
			if ("127.0.0" not in item[0]):
				interfaces.append(item)
				
		network = {
			'interfaces': interfaces
		}
		
		onBootServices = []
		if (self.ServicesDB is not None):
			onBootServices = self.ServicesDB["on_boot_services"]

		payload = {
			'cpu_usage': str(cpuUsage),
			'cpu_temperature': str(temperature),
			'ram_total': str(ramTotal),
			'ram_used': str(ramUsed),
			'ram_available': str(ramAvailable),
			'hd_total': str(hdTotal),
			'hd_used': str(hdUsed),
			'hd_available': str(hdAvailable),
			'os_type': str(osType),
			'board_type': str(boardType),
			'cpu_type': str(cpuType),
			'machine_name': str(machineName),
			'network': network,
			'on_boot_services': onBootServices,
			'network_devices': self.NetworkDevicesList
		}

		return THIS.Node.Network.BasicProtocol.BuildResponse(packet, payload)
	
	def Request_GetServicesInfoHandler(self, sock, packet):
		if self.ServicesDB is None:
			installed = []
		else:
			installed = self.ServicesDB["on_boot_services"]
		payload = {
			'on_boot_services': installed,
		}

		return THIS.Node.Network.BasicProtocol.BuildResponse(packet, payload)
	
	def Request_SetServiceInfoHandler(self, sock, packet):
		self.Node.LogMSG("({classname})# [Request_SetServiceInfoHandler] {0}".format(packet,classname=self.ClassName),5)
		payload = THIS.Node.Network.BasicProtocol.GetPayloadFromJson(packet)
		uuid 	= payload["uuid"]
		enabled = payload["enabled"]
		
		service_found = None
		dbOnBootServices = self.ServicesDB["on_boot_services"]
		for item in dbOnBootServices:
			if (item["uuid"] == uuid):
				item["enabled"] = enabled
				service_found = item
				break
		
		if service_found is not None:
			self.ServicesDB["on_boot_services"] = dbOnBootServices
			# Save new switch to database
			self.File.SaveJSON(os.path.join(self.Node.MKSPath,"services.json"), self.ServicesDB)
			if enabled == 0:
				# Find service need attention
				connections = THIS.Node.GetConnectedNodes()
				service_need_attention = None
				for key in connections:
					node = connections[key]
					if node.Obj["type"] == service_found["type"]:
						service_need_attention = {
							"uuid": node.Obj["uuid"],
							"name": node.Obj["name"],
							"type": node.Obj["type"],
							"pid": node.Obj["pid"]
						}
						break
			else:
				service_need_attention = {
					"uuid": item["uuid"],
					"name": item["name"],
					"type": item["type"],
					"pid": 0
				}
			
			if service_need_attention is not None:
				# Find guardian instance and send message
				connections = THIS.Node.GetConnectedNodes()
				for key in connections:
					node = connections[key]
					if node.Obj["type"] == 2:
						message = THIS.Node.BasicProtocol.BuildRequest("DIRECT", node.Obj["uuid"], THIS.Node.UUID, "services_mngr", {
							"command": "enable",
							"service": {
								"enabled": enabled,
								"uuid": service_need_attention["uuid"],
								"name": service_need_attention["name"],
								"type": service_need_attention["type"],
								"pid": service_need_attention["pid"]
							}
						}, {})
						local_packet  = THIS.Node.BasicProtocol.AppendMagic(message)
						THIS.Node.SocketServer.Send(node.Socket, local_packet)
		
		payload = { 'error': 'ok' }
		return THIS.Node.Network.BasicProtocol.BuildResponse(packet, payload)
		
	def OnApplicationCommandRequestHandler(self, sock, packet):
		command = self.Node.BasicProtocol.GetCommandFromJson(packet)
		if command in self.RequestHandlers:
			return self.RequestHandlers[command](sock, packet)
		
		return THIS.Node.BasicProtocol.BuildResponse(packet, {
			'error': '-1'
		})

	def OnApplicationCommandResponseHandler(self, sock, packet):
		command = self.Node.BasicProtocol.GetCommandFromJson(packet)
		if command in self.ResponseHandlers:
			self.ResponseHandlers[command](sock, packet)
	
	def OnTerminateConnectionHandler(self, conn):
		self.Node.LogMSG("({classname})# [OnTerminateConnectionHandler]".format(classname=self.ClassName),5)
		if self.Shutdown is False:
			if conn.Obj["info"] is not None:
				if conn.Obj["info"]["is_service"] == "True":
					pass
				else:
					self.RemoveFromRunningNodes(conn.Obj["uuid"])
					nodes = self.InstalledNodesDB["installed_nodes"]
					for node in nodes:
						if node["uuid"] == conn.Obj["uuid"]:
							if (node["enabled"] == 1):
								self.Node.LogMSG("({classname})# Start node - {0}".format(node["name"],classname=self.ClassName),5)
								node_path = os.path.join(self.Node.MKSPath,"nodes",str(node["type"]))
								proc = MkSExternalProcess.ExternalProcess()
								proc.CallProcess("python app.py &", node_path, "")
								return

	def WSDataArrivedHandler(self, sock, packet):
		try:
			#print ("(Master Appplication)# [Gateway] Data arrived.")
			command = packet['data']['header']['command']
			return self.RequestHandlers[command](sock, packet)
		except Exception as e:
			self.Node.LogMSG("({classname})# ERROR - Data arrived issue\n(EXEPTION)# {error}".format(
						classname=self.ClassName,
						error=str(e)),3)
	
	def WSConnectedHandler(self):
		self.Node.LogMSG("({classname})# Connection to Gateway was established.".format(classname=self.ClassName),5)

	def WSConnectionClosedHandler(self):
		self.Node.LogMSG("({classname})# Connection to Gateway was lost.".format(classname=self.ClassName),5)
	
	def RemoveFromRunningNodes(self, uuid):
		remove_node = None
		for node in self.RunningNodes:
			if node["uuid"] == uuid:
				remove_node = node
		if remove_node is not None:
			self.RunningNodes.remove(node)

	def LoadNodes(self):
		strNodesJson = self.File.Load(os.path.join(self.Node.MKSPath,"nodes.json"))
		if strNodesJson == "":
			self.Node.LogMSG("({classname})# ERROR - Cannot find nodes.json or it is empty.".format(classname=self.ClassName),3)
			return
		
		self.InstalledNodesDB = json.loads(strNodesJson)
		nodes = self.InstalledNodesDB["installed_nodes"]
		for node in nodes:
			if (node["enabled"] == 1):
				self.Node.LogMSG("({classname})# Start node - {0}".format(node["name"],classname=self.ClassName),5)
				node_path = os.path.join(self.Node.MKSPath,"nodes",str(node["type"]))
				proc = MkSExternalProcess.ExternalProcess()
				proc_str = "python app.py --type {0} &".format(node["type"])
				proc.CallProcess(proc_str, node_path, "")
				#self.RunningNodes.append(node)

	def ShutdownProcess(self):
		self.Shutdown = True
		if self.Installer is not None:
			self.Installer.Stop()
		if self.Uploader is not None:
			self.Uploader.Stop()
		shutdown_connections = []
		# TODO - Could be an issue with not locking this list. (multithreading)
		connections = THIS.Node.GetConnectedNodes()
		for key in connections:
			node = connections[key]
			shutdown_connections.append({
				"sock": node.Socket,
				"uuid": node.Obj["uuid"]
			})
		
		for item in shutdown_connections:
			if item["uuid"] != THIS.Node.UUID:
				message = THIS.Node.BasicProtocol.BuildRequest("DIRECT", item["uuid"], THIS.Node.UUID, "shutdown", {}, {})
				packet  = THIS.Node.BasicProtocol.AppendMagic(message)
				THIS.Node.SocketServer.Send(item["sock"], packet)
		
		print("Shutting down in 5 seconds")
		time.sleep(5)
	
	def OnStreamSocketCreatedHandler(self, name, identity):
		self.Node.LogMSG("({classname})# [OnStreamSocketCreatedHandler] {0} {1}".format(name,str(identity),classname=self.ClassName),5)

	def OnStreamSocketDataHandler(self, name, identity, data):
		self.Node.LogMSG("({classname})# [OnStreamSocketDataHandler] {0} {1}".format(name,data,classname=self.ClassName),5)
		# Response to slave
		self.Node.SendStream(identity, "PONG")
	
	def OnStreamSocketDisconnectedHandler(self, name, identity):
		self.Node.LogMSG("({classname})# [OnStreamSocketDisconnectedHandler] {0} {1}".format(name,str(identity),classname=self.ClassName),5)

	def NodeSystemLoadedHandler(self):
		self.SystemLoaded 	= True
		self.Installer 		= MkSPackageInstaller.Manager(self)
		self.Uploader 		= MkSFileUploader.Manager(self)
		self.Installer.Run()
		self.Uploader.Run()
		# Load all installed nodes
		self.LoadNodes()
		# Load services DB
		strServicesJson = self.File.Load(os.path.join(self.Node.MKSPath,"services.json"))
		if strServicesJson == "":
			self.Node.LogMSG("({classname})# ERROR - Cannot find service.json or it is empty.".format(classname=self.ClassName),3)
			return
		
		self.ServicesDB = json.loads(strServicesJson)
		self.Node.LogMSG("({classname})# Node system was succesfully loaded.".format(classname=self.ClassName),5)

	def PrintConnections(self):
		try:
			self.Node.LogMSG("({classname})# Live ... ({0})".format(self.Node.Ticker, classname=self.ClassName),5)
			self.Node.LogMSG("({classname})# Current connections:".format(classname=self.ClassName),5)

			connections = THIS.Node.GetConnectedNodes()
			for idx, key in enumerate(connections):
				node = connections[key]
				#message = self.Node.BasicProtocol.BuildRequest("DIRECT", item.UUID, self.Node.UUID, "get_node_status", {}, {})
				#packet  = self.Node.BasicProtocol.AppendMagic(message)
				#self.Node.Transceiver.Send({"sock":item.Socket, "packet":packet}) # Response will update "enabled" or "ts" field in local DB
				self.Node.LogMSG("  {0}\t{1}\t{2}\t{3}\t{4}\t{5}".format(str(idx), node.Obj["local_type"], node.Obj["uuid"], node.Obj["listener_port"], node.Obj["type"], node.IP),5)
			
			self.Node.LogMSG("({classname})# Services:".format(classname=self.ClassName),5)
			for idx, key in enumerate(self.Node.Services):
				service = self.Node.Services[key]
				self.Node.LogMSG("  {0}\t{1}\t{2}\t{3}\t{4}".format(str(idx), service["uuid"], service["enabled"], service["registered"], service["name"]),5)
			
			self.Node.LogMSG("({classname})# Local Master connection:".format(classname=self.ClassName),5)
			for idx, key in enumerate(self.Node.MasterManager.Masters):
				master = self.Node.MasterManager.Masters[key]
				self.Node.LogMSG("  {0}\t{1}\t{2}\n\t\t{3}".format(str(idx), master["uuid"], master["ip"], master["nodes"]),5)
				#self.Node.LogMSG("  {0}\t{1}\t{2}\t{3}".format(str(idx), master["ip"], master["conn"]["obj"]["uuid"], master["status"]),5)
			if not self.Node.MasterManager.Masters:
				self.Node.LogMSG("  Empty",5)
		except Exception as e:
			self.Node.LogMSG("({classname})# ERROR - Data arrived issue\n(EXEPTION)# {error}".format(
					classname=self.ClassName,
					error=str(e)),3) 

	def OnNodeWorkTick(self):
		self.Timer.Tick()

Node = MkSMasterNode.MasterNode()
THIS = Context(Node)

def signal_handler(signal, frame):
	THIS.ShutdownProcess()
	THIS.Node.Stop("Accepted signal from other app")

def main():
	signal.signal(signal.SIGINT, signal_handler)

	THIS.Node.SetLocalServerStatus(True)
	THIS.Node.SetWebServiceStatus(True)

	# Node callbacks
	THIS.Node.GatewayDataArrivedCallback			= THIS.WSDataArrivedHandler
	THIS.Node.GatewayConnectedCallback 				= THIS.WSConnectedHandler
	THIS.Node.OnWSConnectionClosed 					= THIS.WSConnectionClosedHandler
	THIS.Node.NodeSystemLoadedCallback				= THIS.NodeSystemLoadedHandler
	THIS.Node.OnApplicationRequestCallback			= THIS.OnApplicationCommandRequestHandler
	THIS.Node.OnApplicationResponseCallback			= THIS.OnApplicationCommandResponseHandler
	THIS.Node.OnTerminateConnectionCallback			= THIS.OnTerminateConnectionHandler
	# Stream sockets events
	THIS.Node.OnStreamSocketCreatedEvent 			= THIS.OnStreamSocketCreatedHandler
	THIS.Node.OnStreamSocketDataEvent 				= THIS.OnStreamSocketDataHandler
	THIS.Node.OnStreamSocketDisconnectedEvent		= THIS.OnStreamSocketDisconnectedHandler

	# Run Node
	THIS.Node.LogMSG("(Master Application)# Start Node ...",5)
	THIS.Node.Run(THIS.OnNodeWorkTick)
	time.sleep(1)

if __name__ == "__main__":
	main()
