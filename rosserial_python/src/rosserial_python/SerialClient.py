#####################################################################
# Software License Agreement (BSD License)
#
# Copyright (c) 2011, Willow Garage, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following
#    disclaimer in the documentation and/or other materials provided
#    with the distribution.
#  * Neither the name of Willow Garage, Inc. nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

__author__ = "mferguson@willowgarage.com (Michael Ferguson)"

import array
import errno
import imp
import io
import multiprocessing
import queue
import socket
import struct
import sys
import threading
import time
from contextlib import contextmanager

from serial import Serial, SerialException, SerialTimeoutException

import roslib
import rospy
from std_msgs.msg import Time
from rosserial_msgs.msg import TopicInfo, Log
from rosserial_msgs.srv import RequestParamRequest, RequestParamResponse

import diagnostic_msgs.msg

ERROR_MISMATCHED_PROTOCOL = "Mismatched protocol version in packet: lost sync or rosserial_python is from different ros release than the rosserial client"
ERROR_NO_SYNC = "no sync with device"
ERROR_PACKET_FAILED = "Packet Failed : Failed to read msg data"

MAX_UDP_PACKET_SIZE = 508

def load_pkg_module(package, directory):
    #check if its in the python path
    path = sys.path
    try:
        imp.find_module(package)
    except ImportError:
        roslib.load_manifest(package)
    try:
        m = __import__( package + '.' + directory )
    except ImportError:
        rospy.logerr( "Cannot import package : %s"% package )
        rospy.logerr( "sys.path was " + str(path) )
        return None
    return m

def load_message(package, message):
    m = load_pkg_module(package, 'msg')
    m2 = getattr(m, 'msg')
    return getattr(m2, message)

def load_service(package,service):
    s = load_pkg_module(package, 'srv')
    s = getattr(s, 'srv')
    srv = getattr(s, service)
    mreq = getattr(s, service+"Request")
    mres = getattr(s, service+"Response")
    return srv,mreq,mres

@contextmanager
def acquire_timeout(lock, timeout):
    result = lock.acquire(timeout=timeout)
    try:
        yield result
    finally:
        if result:
            lock.release()


class Publisher:
    """
        Publisher forwards messages from the serial device to ROS.
    """
    def __init__(self, topic_info):
        """ Create a new publisher. """
        self.topic = topic_info.topic_name

        # find message type
        package, message = topic_info.message_type.split('/')
        self.message = load_message(package, message)
        if self.message._md5sum == topic_info.md5sum:
            self.publisher = rospy.Publisher(self.topic, self.message, queue_size=10)
        else:
            raise Exception('Checksum does not match: ' + self.message._md5sum + ',' + topic_info.md5sum)

    def handlePacket(self, data):
        """ Forward message to ROS network. """
        try:
            m = self.message()
            m.deserialize(data)
            self.publisher.publish(m)
        except Exception as e:
            rospy.logerr("Publisher handling packet failed: %s", e)


class Subscriber:
    """
        Subscriber forwards messages from ROS to the serial device.
    """

    def __init__(self, topic_info, parent):
        self.topic = topic_info.topic_name
        self.id = topic_info.topic_id
        self.parent = parent

        # find message type
        package, message = topic_info.message_type.split('/')
        self.message = load_message(package, message)
        if self.message._md5sum == topic_info.md5sum:
            self.subscriber = rospy.Subscriber(self.topic, self.message, self.callback)
        else:
            raise Exception('Checksum does not match: ' + self.message._md5sum + ',' + topic_info.md5sum)

    def callback(self, msg):
        """ Forward message to serial device. """
        data_buffer = io.BytesIO()
        msg.serialize(data_buffer)
        self.parent.send(self.id, data_buffer.getvalue())

    def unregister(self):
        rospy.loginfo("Removing subscriber: %s", self.topic)
        self.subscriber.unregister()

class ServiceServer:
    """
        ServiceServer responds to requests from ROS.
    """

    def __init__(self, topic_info, parent):
        self.topic = topic_info.topic_name
        self.parent = parent

        # find message type
        package, service = topic_info.message_type.split('/')
        s = load_pkg_module(package, 'srv')
        s = getattr(s, 'srv')
        self.mreq = getattr(s, service+"Request")
        self.mres = getattr(s, service+"Response")
        srv = getattr(s, service)
        self.service = rospy.Service(self.topic, srv, self.callback)

        # response message
        self.data = None

    def unregister(self):
        rospy.loginfo("Removing service: %s", self.topic)
        self.service.shutdown()

    def callback(self, req):
        """ Forward request to serial device. """
        data_buffer = io.BytesIO()
        req.serialize(data_buffer)
        self.response = None
        self.parent.send(self.id, data_buffer.getvalue())
        while self.response is None:
            pass
        return self.response

    def handlePacket(self, data):
        """ Forward response to ROS network. """
        try:
            r = self.mres()
            r.deserialize(data)
            self.response = r
        except Exception as e:
            rospy.logerr("Service server handling packet failed: %s", e)


class ServiceClient:
    """
        ServiceServer responds to requests from ROS.
    """

    def __init__(self, topic_info, parent):
        self.topic = topic_info.topic_name
        self.parent = parent

        # find message type
        package, service = topic_info.message_type.split('/')
        s = load_pkg_module(package, 'srv')
        s = getattr(s, 'srv')
        self.mreq = getattr(s, service+"Request")
        self.mres = getattr(s, service+"Response")
        srv = getattr(s, service)
        rospy.loginfo("Starting service client, waiting for service '" + self.topic + "'")
        rospy.wait_for_service(self.topic)
        self.proxy = rospy.ServiceProxy(self.topic, srv)

    def handlePacket(self, data):
        """ Forward request to ROS network. """
        try:
            req = self.mreq()
            req.deserialize(data)
            # call service proxy
            resp = self.proxy(req)
            # serialize and publish
            data_buffer = io.BytesIO()
            resp.serialize(data_buffer)
            self.parent.send(self.id, data_buffer.getvalue())
        except Exception as e:
            rospy.logerr("Service client handling packet failed: %s", e)


class RosSerialServer:
    """
        RosSerialServer waits for a socket connection then passes itself, forked as a
        new process, to SerialClient which uses it as a serial port. It continues to listen
        for additional connections. Each forked process is a new ros node, and proxies ros
        operations (e.g. publish/subscribe) from its connection to the rest of ros.
    """
    def __init__(self, tcp_portnum, fork_server=False):
        rospy.loginfo("Fork_server is: %s" % fork_server)
        self.tcp_portnum = tcp_portnum
        self.fork_server = fork_server

    def listen(self):
        self.serversocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.serversocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # get buffer size
        rospy.loginfo("Getting socket buffer size")
        bufsize = self.serversocket.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        rospy.loginfo("Socket buffer size: %d bytes" % bufsize)
        # increase socket buffer size to 500KB
        self.serversocket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 512000)
        newbufsize = self.serversocket.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        rospy.loginfo("New Socket buffer size: %d bytes" % newbufsize)
        #bind the socket to a public host, and a well-known port
        self.serversocket.bind(("", self.tcp_portnum)) #become a server socket
        self.serversocket.listen(1)
        self.serversocket.settimeout(1)

        #accept connections
        rospy.loginfo("Waiting for socket connection")
        while not rospy.is_shutdown():
            try:
                clientsocket, address = self.serversocket.accept()
            except socket.timeout:
                continue

            #now do something with the clientsocket
            rospy.loginfo("Established a socket connection from %s on port %s" % address)
            self.socket = clientsocket
            self.socket.settimeout(5.0)
            self.isConnected = True

            if self.fork_server: # if configured to launch server in a separate process
                rospy.loginfo("Forking a socket server process")
                process = multiprocessing.Process(target=self.startSocketServer, args=address)
                process.daemon = True
                process.start()
                rospy.loginfo("launched startSocketServer")
            else:
                rospy.loginfo("calling startSerialClient")
                self.startSerialClient()
                rospy.loginfo("startSerialClient() exited")

    def startSerialClient(self):
        client = SerialClient(self)
        try:
            client.run()
        except KeyboardInterrupt as e:
            rospy.loginfo(f"{e}")
        except RuntimeError:
            rospy.loginfo("RuntimeError exception caught")
            self.isConnected = False
        except socket.error:
            rospy.loginfo("socket.error exception caught")
            self.isConnected = False
        finally:
            rospy.loginfo("Client has exited, closing socket.")
            self.socket.close()
            for sub in client.subscribers.values():
                sub.unregister()
            for srv in client.services.values():
                srv.unregister()

    def startSocketServer(self, port, address):
        rospy.loginfo("starting ROS Serial Python Node serial_node-%r" % address)
        rospy.init_node("serial_node_%r" % address)
        self.startSerialClient()

    def flushInput(self):
        pass

    def write(self, data):
        if not self.isConnected:
            return
        length = len(data)
        totalsent = 0

        while totalsent < length:
            try:
                totalsent += self.socket.send(data[totalsent:])
            except BrokenPipeError:
                raise RuntimeError("RosSerialServer.write() socket connection broken")

    def read(self, rqsted_length):
        self.msg = b''
        if not self.isConnected:
            return self.msg

        while len(self.msg) < rqsted_length:
            chunk = self.socket.recv(rqsted_length - len(self.msg))
            if chunk == b'':
                raise RuntimeError("RosSerialServer.read() socket connection broken")
            self.msg = self.msg + chunk
        return self.msg

    def inWaiting(self):
        try: # the caller checks just for <1, so we'll peek at just one byte
            chunk = self.socket.recv(1, socket.MSG_DONTWAIT|socket.MSG_PEEK)
            if chunk == b'':
                raise RuntimeError("RosSerialServer.inWaiting() socket connection broken")
            return len(chunk)
        except BlockingIOError:
            return 0

class RosSerialUDPServer:
    """
        RosSerialUDPServer waits for a UDP packet then passes itself to SerialClient, which
        uses it as a serial port. It listens for additional packets. Each process proxies ROS
        operations (e.g. publish/subscribe) from its connection to the rest of ROS.
    """
    def __init__(self, udp_portnum, fork_server=False):
        rospy.loginfo("Fork_server is: %s" % fork_server)
        self.udp_portnum = udp_portnum
        self.fork_server = fork_server
        self.recv_buffer  = b'' # Buffer to store leftover data from packets

    def listen(self):
        self.serversocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.serversocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Get buffer size
        rospy.loginfo("Getting socket buffer size")
        bufsize = self.serversocket.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        rospy.loginfo("Socket buffer size: %d bytes" % bufsize)
        # Increase socket buffer size to 500KB
        self.serversocket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 512000)
        newbufsize = self.serversocket.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        rospy.loginfo("New Socket buffer size: %d bytes" % newbufsize)
        # Bind the socket to a public host and a well-known port
        self.serversocket.bind(("", self.udp_portnum))  # become a UDP server socket
        rospy.loginfo("UDP server listening on port %d" % self.udp_portnum)

        # Set socket timeout
        self.serversocket.settimeout(5)

        self.isConnected = False

        self.client_address = None

        # Listen for UDP packets
        while not rospy.is_shutdown():
            try:
                # Check if there is data to be received 
                data, address = self.serversocket.recvfrom(1, socket.MSG_PEEK)

                # If this is the first connection, store the address
                if self.client_address is None:
                    self.client_address = address
                    rospy.loginfo(f"Client connected from {self.client_address}")
                    self.isConnected = True

                if self.fork_server:  # If configured to launch server in a separate process
                    rospy.loginfo("Forking a socket server process")
                    process = multiprocessing.Process(target=self.startSocketServer, args=(address,))
                    process.daemon = True
                    process.start()
                    rospy.loginfo("Launched startSocketServer")
                else:
                    rospy.loginfo("Calling startSerialClient")
                    self.startSerialClient()
                    rospy.loginfo("startSerialClient() exited")
            except socket.timeout:
                self.isConnected = False
                self.client_address = None
                continue

    def startSerialClient(self):
        client = SerialClient(self)
        try:
            client.run()
        except KeyboardInterrupt as e:
            rospy.loginfo(f"{e}")
        except RuntimeError:
            rospy.loginfo("RuntimeError exception caught")
            self.isConnected = False
            self.client_address = None
        finally:
            rospy.loginfo("Client has exited.")

    def startSocketServer(self, address):
        rospy.loginfo("Starting ROS Serial Python Node serial_node-%r" % address)
        rospy.init_node("serial_node_%r" % address)
        self.startSerialClient()

    def flushInput(self):
        self.recv_buffer  = b''

    def write(self, data):
        if not self.isConnected or self.client_address is None:
            return

        total_length = len(data)
        offset = 0

        while offset < total_length:
            # Determine the size of the next chunk
            chunk_size = min(MAX_UDP_PACKET_SIZE, total_length - offset)
            chunk = data[offset:offset + chunk_size]
            
            try:
                # Send the chunk to the stored client address
                self.serversocket.sendto(chunk, self.client_address)
                offset += chunk_size
            except BrokenPipeError:
                raise RuntimeError("RosSerialServerUDP.write() socket connection broken")

    def read(self, rqsted_length):
        self.msg = b''  # Buffer to accumulate the received message

        if not self.isConnected or self.client_address is None:
            return self.msg  # Return an empty message if not connected

        # First, try to use any leftover data in the internal buffer
        if self.recv_buffer:
            # Calculate how much data can be used from the buffer
            to_read = min(len(self.recv_buffer), rqsted_length)
            self.msg += self.recv_buffer[:to_read]
            self.recv_buffer = self.recv_buffer[to_read:]

        while len(self.msg) < rqsted_length:
            chunk, address = self.serversocket.recvfrom(4096)

            # Check if the connection is broken
            if chunk == b'':
                raise RuntimeError("RosSerialServer.read() socket connection broken")

            
            # Check if data is coming from the client address
            if address != self.client_address:
                # If data comes from the same IP but different port, update the client_address
                if address[0] == self.client_address[0]:
                    rospy.logwarn(f"Updating client address from {self.client_address} to {address}")
                    self.client_address = address
                else:
                    rospy.loginfo(f"Ignoring packet from unauthorized address {address}")
                    continue

            
            # Determine how much of the chunk can be added to `self.msg`
            to_add = rqsted_length - len(self.msg)
            self.msg += chunk[:to_add]

            # Store any remaining data from the chunk in the internal buffer
            if len(chunk) > to_add:
                self.recv_buffer += chunk[to_add:]

        return self.msg

    def inWaiting(self):
        try:
            chunk, address = self.serversocket.recvfrom(4096, socket.MSG_DONTWAIT)
            if chunk == b'':
                raise RuntimeError("RosSerialServerUDP.inWaiting() socket connection broken")

            # Check if data is coming from the client address
            if address != self.client_address:
                # If data comes from the same IP but different port, update the client_address
                if address[0] == self.client_address[0]:
                    rospy.logwarn(f"Updating client address from {self.client_address} to {address}")
                    self.client_address = address
                else:
                    rospy.loginfo(f"Ignoring packet from unauthorized address {address}")

            self.recv_buffer += chunk
        except BlockingIOError:
            pass

        return len(self.recv_buffer)


class SerialClient(object):
    """
        ServiceServer responds to requests from the serial device.
    """
    header = b'\xff'

    # hydro introduces protocol ver2 which must match node_handle.h
    # The protocol version is sent as the 2nd sync byte emitted by each end
    protocol_ver1 = b'\xff'
    protocol_ver2 = b'\xfe'
    protocol_ver = protocol_ver2

    def __init__(self, port=None, baud=57600, timeout=5.0, fix_pyserial_for_test=False):
        """ Initialize node, connect to bus, attempt to negotiate topics. """

        self.read_lock = threading.RLock()

        self.write_lock = threading.RLock()
        self.write_queue = queue.Queue()
        self.write_thread = None

        self.lastsync = rospy.Time(0)
        self.lastsync_lost = rospy.Time(0)
        self.lastsync_success = rospy.Time(0)
        self.last_read = rospy.Time(0)
        self.last_write = rospy.Time(0)
        self.timeout = timeout
        self.synced = False
        self.fix_pyserial_for_test = fix_pyserial_for_test

        self.publishers = dict()  # id:Publishers
        self.subscribers = dict() # topic:Subscriber
        self.services = dict()    # topic:Service
        
        def shutdown():
            self.txStopRequest()
            rospy.loginfo('shutdown hook activated')
        rospy.on_shutdown(shutdown)
        
        self.pub_diagnostics = rospy.Publisher('/diagnostics', diagnostic_msgs.msg.DiagnosticArray, queue_size=10)

        if port is None:
            # no port specified, listen for any new port?
            pass
        elif hasattr(port, 'read'):
            #assume its a filelike object
            self.port=port
        else:
            # open a specific port
            while not rospy.is_shutdown():
                try:
                    if self.fix_pyserial_for_test:
                        # see https://github.com/pyserial/pyserial/issues/59
                        self.port = Serial(port, baud, timeout=self.timeout, write_timeout=10, rtscts=True, dsrdtr=True)
                    else:
                        self.port = Serial(port, baud, timeout=self.timeout, write_timeout=10)
                    break
                except SerialException as e:
                    rospy.logerr("Error opening serial: %s", e)
                    time.sleep(3)

        if rospy.is_shutdown():
            return

        time.sleep(0.1)           # Wait for ready (patch for Uno)

        self.buffer_out = -1
        self.buffer_in = -1

        self.callbacks = dict()
        # endpoints for creating new pubs/subs
        self.callbacks[TopicInfo.ID_PUBLISHER] = self.setupPublisher
        self.callbacks[TopicInfo.ID_SUBSCRIBER] = self.setupSubscriber
        # service client/servers have 2 creation endpoints (a publisher and a subscriber)
        self.callbacks[TopicInfo.ID_SERVICE_SERVER+TopicInfo.ID_PUBLISHER] = self.setupServiceServerPublisher
        self.callbacks[TopicInfo.ID_SERVICE_SERVER+TopicInfo.ID_SUBSCRIBER] = self.setupServiceServerSubscriber
        self.callbacks[TopicInfo.ID_SERVICE_CLIENT+TopicInfo.ID_PUBLISHER] = self.setupServiceClientPublisher
        self.callbacks[TopicInfo.ID_SERVICE_CLIENT+TopicInfo.ID_SUBSCRIBER] = self.setupServiceClientSubscriber
        # custom endpoints
        self.callbacks[TopicInfo.ID_PARAMETER_REQUEST] = self.handleParameterRequest
        self.callbacks[TopicInfo.ID_LOG] = self.handleLoggingRequest
        self.callbacks[TopicInfo.ID_TIME] = self.handleTimeRequest

        rospy.sleep(2.0)
        self.requestTopics()
        self.lastsync = rospy.Time.now()

    def requestTopics(self):
        """ Determine topics to subscribe/publish. """
        rospy.loginfo('Requesting topics...')

        # TODO remove if possible
        if not self.fix_pyserial_for_test:
            with self.read_lock:
                try:
                    self.port.flushInput()
                except AttributeError: # socket doesn't have flushInput
                    pass

        # request topic sync
        self.write_queue.put(self.header + self.protocol_ver + b"\x00\x00\xff\x00\x00\xff")

    def txStopRequest(self):
        """ Send stop tx request to client before the node exits. """
        if not self.fix_pyserial_for_test:
            with self.read_lock:
                try:
                    self.port.flushInput()
                except AttributeError: # socket doesn't have flushInput
                    pass

        self.write_queue.put(self.header + self.protocol_ver + b"\x00\x00\xff\x0b\x00\xf4")
        rospy.loginfo("Sending tx stop request")

    def tryRead(self, length):
        try:
            read_start = time.time()
            bytes_remaining = length
            result = bytearray()
            while bytes_remaining != 0 and time.time() - read_start < self.timeout:
                with self.read_lock:
                    received = self.port.read(bytes_remaining)
                if len(received) != 0:
                    self.last_read = rospy.Time.now()
                    result.extend(received)
                    bytes_remaining -= len(received)

            if bytes_remaining != 0:
                raise IOError("Returned short (expected %d bytes, received %d instead)." % (length, length - bytes_remaining))

            return bytes(result)
        except Exception as e:
            raise IOError("Serial Port read failure: %s" % e)

    def run(self):
        """ Forward recieved messages to appropriate publisher. """

        # Launch write thread.
        if self.write_thread is None:
            self.write_thread = threading.Thread(target=self.processWriteQueue)
            self.write_thread.daemon = True
            self.write_thread.start()

        # Handle reading.
        data = ''
        read_step = None
        while self.write_thread.is_alive() and not rospy.is_shutdown():
            if (rospy.Time.now() - self.lastsync).to_sec() > (self.timeout * 3):
                if self.synced:
                    rospy.logerr("Lost sync with device, restarting NOW...")
                    return
                else:
                    rospy.logerr("Unable to sync with device; possible link problem or link software version mismatch such as hydro rosserial_python with groovy Arduino")
                self.lastsync_lost = rospy.Time.now()
                self.sendDiagnostics(diagnostic_msgs.msg.DiagnosticStatus.ERROR, ERROR_NO_SYNC)
                self.requestTopics()
                self.lastsync = rospy.Time.now()

            # This try-block is here because we make multiple calls to read(). Any one of them can throw
            # an IOError if there's a serial problem or timeout. In that scenario, a single handler at the
            # bottom attempts to reconfigure the topics.
            try:
                with acquire_timeout(self.read_lock, 1) as res:
                    if res:
                        is_empty = self.port.inWaiting() < 1
                        if is_empty:
                            time.sleep(0.001)
                            continue
                    else:
                        continue

                # Find sync flag.
                flag = [0, 0]
                read_step = 'syncflag'
                flag[0] = self.tryRead(1)
                if (flag[0] != self.header):
                    continue

                # Find protocol version.
                read_step = 'protocol'
                flag[1] = self.tryRead(1)
                if flag[1] != self.protocol_ver:
                    self.sendDiagnostics(diagnostic_msgs.msg.DiagnosticStatus.ERROR, ERROR_MISMATCHED_PROTOCOL)
                    rospy.logerr("Mismatched protocol version in packet (%s): lost sync or rosserial_python is from different ros release than the rosserial client" % repr(flag[1]))
                    protocol_ver_msgs = {
                            self.protocol_ver1: 'Rev 0 (rosserial 0.4 and earlier)',
                            self.protocol_ver2: 'Rev 1 (rosserial 0.5+)',
                            b'\xfd': 'Some future rosserial version'
                    }
                    if flag[1] in protocol_ver_msgs:
                        found_ver_msg = 'Protocol version of client is ' + protocol_ver_msgs[flag[1]]
                    else:
                        found_ver_msg = "Protocol version of client is unrecognized"
                    rospy.loginfo("%s, expected %s" % (found_ver_msg, protocol_ver_msgs[self.protocol_ver]))
                    continue

                # Read message length, checksum (3 bytes)
                read_step = 'message length'
                msg_len_bytes = self.tryRead(3)
                msg_length, _ = struct.unpack("<hB", msg_len_bytes)

                # Validate message length checksum.
                if sum(array.array("B", msg_len_bytes)) % 256 != 255:
                    rospy.loginfo("Wrong checksum for msg length, length %d, dropping message." % (msg_length))
                    continue

                # Read topic id (2 bytes)
                read_step = 'topic id'
                topic_id_header = self.tryRead(2)
                topic_id, = struct.unpack("<H", topic_id_header)

                # Read serialized message data.
                read_step = 'data'
                try:
                    msg = self.tryRead(msg_length)
                except IOError as e:
                    self.sendDiagnostics(diagnostic_msgs.msg.DiagnosticStatus.ERROR, ERROR_PACKET_FAILED)
                    rospy.loginfo("Packet Failed :  Failed to read msg data")
                    rospy.loginfo("expected msg length is %d", msg_length)
                    rospy.loginfo(e)
                    raise

                # Reada checksum for topic id and msg
                read_step = 'data checksum'
                chk = self.tryRead(1)
                checksum = sum(array.array('B', topic_id_header + msg + chk))

                # Validate checksum.
                if checksum % 256 == 255:
                    self.synced = True
                    self.lastsync_success = rospy.Time.now()
                    try:
                        self.callbacks[topic_id](msg)
                    except KeyError:
                        rospy.logerr("Tried to publish before configured, topic id %d" % topic_id)
                        self.requestTopics()
                    time.sleep(0.001)
                else:
                    rospy.loginfo("wrong checksum for topic id and msg")

            except IOError as exc:
                rospy.logwarn('Last read step: %s' % read_step)
                rospy.logwarn('Run loop error: %s' % exc)
                return
        self.write_thread.join()

    def setPublishSize(self, size):
        if self.buffer_out < 0:
            self.buffer_out = size
            rospy.loginfo("Note: publish buffer size is %d bytes" % self.buffer_out)

    def setSubscribeSize(self, size):
        if self.buffer_in < 0:
            self.buffer_in = size
            rospy.loginfo("Note: subscribe buffer size is %d bytes" % self.buffer_in)

    def setupPublisher(self, data):
        """ Register a new publisher. """
        try:
            msg = TopicInfo()
            msg.deserialize(data)
            pub = Publisher(msg)
            if msg.topic_id not in self.publishers:
                rospy.loginfo("Setup publisher on %s [%s]" % (msg.topic_name, msg.message_type) )
            self.publishers[msg.topic_id] = pub
            self.callbacks[msg.topic_id] = pub.handlePacket
            self.setPublishSize(msg.buffer_size)
            
        except Exception as e:
            rospy.logerr("Creation of publisher failed: %s", e)

    def setupSubscriber(self, data):
        """ Register a new subscriber. """
        try:
            msg = TopicInfo()
            msg.deserialize(data)
            if not msg.topic_name in list(self.subscribers.keys()):
                sub = Subscriber(msg, self)
                self.subscribers[msg.topic_name] = sub
                self.setSubscribeSize(msg.buffer_size)
                rospy.loginfo("Setup subscriber on %s [%s]" % (msg.topic_name, msg.message_type) )
            elif msg.message_type != self.subscribers[msg.topic_name].message._type:
                old_message_type = self.subscribers[msg.topic_name].message._type
                self.subscribers[msg.topic_name].unregister()
                sub = Subscriber(msg, self)
                self.subscribers[msg.topic_name] = sub
                self.setSubscribeSize(msg.buffer_size)
                rospy.loginfo("Change the message type of subscriber on %s from [%s] to [%s]" % (msg.topic_name, old_message_type, msg.message_type) )
        except Exception as e:
            rospy.logerr("Creation of subscriber failed: %s", e)

    def setupServiceServerPublisher(self, data):
        """ Register a new service server. """
        try:
            msg = TopicInfo()
            msg.deserialize(data)
            self.setPublishSize(msg.buffer_size)
            try:
                srv = self.services[msg.topic_name]
            except KeyError:
                srv = ServiceServer(msg, self)
                rospy.loginfo("Setup service server on %s [%s]" % (msg.topic_name, msg.message_type) )
                self.services[msg.topic_name] = srv
            if srv.mres._md5sum == msg.md5sum:
                self.callbacks[msg.topic_id] = srv.handlePacket
            else:
                raise Exception('Checksum does not match: ' + srv.mres._md5sum + ',' + msg.md5sum)
        except Exception as e:
            rospy.logerr("Creation of service server failed: %s", e)

    def setupServiceServerSubscriber(self, data):
        """ Register a new service server. """
        try:
            msg = TopicInfo()
            msg.deserialize(data)
            self.setSubscribeSize(msg.buffer_size)
            try:
                srv = self.services[msg.topic_name]
            except KeyError:
                srv = ServiceServer(msg, self)
                rospy.loginfo("Setup service server on %s [%s]" % (msg.topic_name, msg.message_type) )
                self.services[msg.topic_name] = srv
            if srv.mreq._md5sum == msg.md5sum:
                srv.id = msg.topic_id
            else:
                raise Exception('Checksum does not match: ' + srv.mreq._md5sum + ',' + msg.md5sum)
        except Exception as e:
            rospy.logerr("Creation of service server failed: %s", e)

    def setupServiceClientPublisher(self, data):
        """ Register a new service client. """
        try:
            msg = TopicInfo()
            msg.deserialize(data)
            self.setPublishSize(msg.buffer_size)
            try:
                srv = self.services[msg.topic_name]
            except KeyError:
                srv = ServiceClient(msg, self)
                rospy.loginfo("Setup service client on %s [%s]" % (msg.topic_name, msg.message_type) )
                self.services[msg.topic_name] = srv
            if srv.mreq._md5sum == msg.md5sum:
                self.callbacks[msg.topic_id] = srv.handlePacket
            else:
                raise Exception('Checksum does not match: ' + srv.mreq._md5sum + ',' + msg.md5sum)
        except Exception as e:
            rospy.logerr("Creation of service client failed: %s", e)

    def setupServiceClientSubscriber(self, data):
        """ Register a new service client. """
        try:
            msg = TopicInfo()
            msg.deserialize(data)
            self.setSubscribeSize(msg.buffer_size)
            try:
                srv = self.services[msg.topic_name]
            except KeyError:
                srv = ServiceClient(msg, self)
                rospy.loginfo("Setup service client on %s [%s]" % (msg.topic_name, msg.message_type) )
                self.services[msg.topic_name] = srv
            if srv.mres._md5sum == msg.md5sum:
                srv.id = msg.topic_id
            else:
                raise Exception('Checksum does not match: ' + srv.mres._md5sum + ',' + msg.md5sum)
        except Exception as e:
            rospy.logerr("Creation of service client failed: %s", e)

    def handleTimeRequest(self, data):
        """ Respond to device with system time. """
        t = Time()
        t.data = rospy.Time.now()
        data_buffer = io.BytesIO()
        t.serialize(data_buffer)
        self.send( TopicInfo.ID_TIME, data_buffer.getvalue() )
        self.lastsync = rospy.Time.now()

    def handleParameterRequest(self, data):
        """ Send parameters to device. Supports only simple datatypes and arrays of such. """
        try:
            req = RequestParamRequest()
            req.deserialize(data)
            resp = RequestParamResponse()
        except Exception as e:
            rospy.logerr("Handle Parameter Request failed: %s", e)
            return

        resp.name = req.name
        param_exists = False
        try:
            param = rospy.get_param(req.name)
            if param is not None:
                param_exists = True
                
        except KeyError:
            pass

        if param_exists:
            if isinstance(param, dict):
                rospy.logerr("Cannot send param %s because it is a dictionary"%req.name)
            else:
                if not isinstance(param, list):
                    param = [param]

                #check to make sure that all parameters in list are same type
                t = type(param[0])
                for p in param:
                    if t!= type(p):
                        rospy.logerr('All Parameters in the list %s must be of the same type'%req.name)
                        break
                else:
                    if t == int or t == bool:
                        resp.ints = param
                    if t == float:
                        resp.floats =param
                    if t == str:
                        resp.strings = param


                    resp.exists = True
                    rospy.loginfo('Requesting param %s'%req.name)
                    
        else:
            rospy.logerr("Parameter %s does not exist"%req.name)

        data_buffer = io.BytesIO()
        resp.serialize(data_buffer)
        self.send(TopicInfo.ID_PARAMETER_REQUEST, data_buffer.getvalue())

    def handleLoggingRequest(self, data):
        """ Forward logging information from serial device into ROS. """
        try:
            msg = Log()
            msg.deserialize(data)
            if msg.level == Log.ROSDEBUG:
                rospy.logdebug(msg.msg)
            elif msg.level == Log.INFO:
                rospy.loginfo(msg.msg)
            elif msg.level == Log.WARN:
                rospy.logwarn(msg.msg)
            elif msg.level == Log.ERROR:
                rospy.logerr(msg.msg)
            elif msg.level == Log.FATAL:
                rospy.logfatal(msg.msg)
        except Exception as e:
            rospy.logerr("Handling Logging Request failed: %s", e)

    def send(self, topic, msg):
        """
        Queues data to be written to the serial port.
        """
        self.write_queue.put((topic, msg))

    def _write(self, data):
        """
        Writes raw data over the serial port. Assumes the data is formatting as a packet. http://wiki.ros.org/rosserial/Overview/Protocol
        """
        with self.write_lock:
            self.port.write(data)
            self.last_write = rospy.Time.now()

    def _send(self, topic, msg_bytes):
        """
        Send a message on a particular topic to the device.
        """
        length = len(msg_bytes)
        if self.buffer_in > 0 and length > self.buffer_in:
            rospy.logerr("Message from ROS network dropped: message larger than buffer.\n%s" % msg)
            return -1
        else:
            # frame : header (1b) + version (1b) + msg_len(2b) + msg_len_chk(1b) + topic_id(2b) + msg(nb) + msg_topic_id_chk(1b)
            length_bytes = struct.pack('<h', length)
            length_checksum = 255 - (sum(array.array('B', length_bytes)) % 256)
            length_checksum_bytes = struct.pack('B', length_checksum)

            topic_bytes = struct.pack('<h', topic)
            msg_checksum = 255 - (sum(array.array('B', topic_bytes + msg_bytes)) % 256)
            msg_checksum_bytes = struct.pack('B', msg_checksum)

            self._write(self.header + self.protocol_ver + length_bytes + length_checksum_bytes + topic_bytes + msg_bytes + msg_checksum_bytes)
            return length

    def processWriteQueue(self):
        """
        Main loop for the thread that processes outgoing data to write to the serial port.
        """
        while not rospy.is_shutdown():
            if self.write_queue.empty():
                time.sleep(0.01)
            else:
                data = self.write_queue.get()
                while True:
                    try:
                        if isinstance(data, tuple):
                            topic, msg = data
                            self._send(topic, msg)
                        elif isinstance(data, bytes):
                            self._write(data)
                        else:
                            rospy.logerr("Trying to write invalid data type: %s" % type(data))
                        break
                    except SerialTimeoutException as exc:
                        rospy.logerr('Write timeout: %s' % exc)
                        time.sleep(1)
                    except RuntimeError as exc:
                        rospy.logerr('Write thread exception: %s' % exc)
                        break


    def sendDiagnostics(self, level, msg_text):
        msg = diagnostic_msgs.msg.DiagnosticArray()
        status = diagnostic_msgs.msg.DiagnosticStatus()
        status.name = "rosserial_python"
        msg.header.stamp = rospy.Time.now()
        msg.status.append(status)

        status.message = msg_text
        status.level = level

        status.values.append(diagnostic_msgs.msg.KeyValue())
        status.values[0].key="last sync"
        if self.lastsync.to_sec()>0:
            status.values[0].value=time.ctime(self.lastsync.to_sec())
        else:
            status.values[0].value="never"

        status.values.append(diagnostic_msgs.msg.KeyValue())
        status.values[1].key="last sync lost"
        status.values[1].value=time.ctime(self.lastsync_lost.to_sec())

        self.pub_diagnostics.publish(msg)
