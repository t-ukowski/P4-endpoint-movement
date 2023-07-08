#!/usr/bin/env python3
import argparse
import os
import sys
from time import sleep
import time
import grpc
import socket
from threading import Thread
import threading

sys.path.append(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 '../../utils/'))
import p4runtime_lib.bmv2
import p4runtime_lib.helper
from p4runtime_lib.error_utils import printGrpcError
from p4runtime_lib.switch import ShutdownAllSwitchConnections
from p4.v1 import p4runtime_pb2, p4runtime_pb2_grpc

switch_mac_table = {
    "s1": "08:00:00:00:01:00",
    "s2": "08:00:00:00:02:00",
    "s3": "08:00:00:00:03:00",
    "s4": "08:00:00:00:04:00"
}

switch_ports = {
    "s1": {"s2": 2, "s3": 3, "s4": 3},
    "s2": {"s1": 2, "s3": 3, "s4": 3},
    "s3": {"s1": 2, "s2": 3, "s4": 4},
    "s4": {"s1": 2, "s2": 2, "s3": 2}
}

def readTableRules(sw):
    """
    Reads the table entries from all tables on the switch.

    :param sw: the switch connection
    """
    print('\n----- Reading tables rules for %s -----' % sw.name)
    for response in sw.ReadTableEntries():
        for entity in response.entities:
            entry = entity.table_entry
            print(entry)
            print('-----')

def handleBounceEntry(p4info_helper, host, new_switch_name, old_switch_name, switches):
    """
    Function to handle bounce entry creation and removal
    """
    print(f"Creating bounce entry for migrating host: {host}")

    for switch in switches:
        if switch.name == old_switch_name:
            table_entry = p4info_helper.buildTableEntry(
                table_name="MyIngress.ipv4_lpm",
                match_fields={
                    "hdr.ipv4.dstAddr": (host, 32)
                },
                action_name="MyIngress.ipv4_forward",
                action_params={
                    "dstAddr": switch_mac_table[new_switch_name],
                    "port": switch_ports[old_switch_name][new_switch_name]
                })
            switch.WriteTableEntry(table_entry)
            print("Installed bounce entry for host {} on switch {}".format(host, old_switch_name))
            break


def monitorHostLocation(switch, added_entries_shared, removed_entries_shared):

    """
    Monitors the host location by continuously reading the table entries and
    comparing them with the expected rules.
    """
    it = 0
    expected_table_entries = []
    while True:
        current_table_entries = []
        for response in switch.ReadTableEntries():
            for entity in response.entities:
                entry = entity.table_entry
                current_table_entries.append(entry)

        if current_table_entries != expected_table_entries and it != 0:

            added_entries = [entry for entry in current_table_entries if not entry in expected_table_entries]
            removed_entries = [entry for entry in expected_table_entries if not entry in current_table_entries]
            for entry in added_entries:
                ip_in_bytes = get_ipv4_dst_address(entry)
                ip_in_dotted_decimal = socket.inet_ntoa(ip_in_bytes)
                print('A new entry has been added on switch {}. IP: {}'.format(switch.name, ip_in_dotted_decimal))

                added_entries_shared[ip_in_dotted_decimal] = switch.name

            if len(added_entries) == 0:
                for entry in removed_entries:
                    ip_in_bytes = get_ipv4_dst_address(entry)
                    ip_in_dotted_decimal = socket.inet_ntoa(ip_in_bytes)
                    print('An entry has been removed from switch {}. IP: {}'.format(switch.name, ip_in_dotted_decimal))

                    removed_entries_shared[ip_in_dotted_decimal] = switch.name

            expected_table_entries = current_table_entries

        if it == 0:
            expected_table_entries = current_table_entries

        sleep(2)
        it += 1

def checkForMigration(p4info_helper, added_entries_shared, removed_entries_shared, switches):
    """
    This function checks for migrations
    """
    while True:
        for host in list(added_entries_shared.keys()):
            if host in list(removed_entries_shared.keys()):
                print(f'Migrating endpoint detected, host: {host} from switch {removed_entries_shared[host]} to switch {added_entries_shared[host]}')

                # Remove the entries from the shared dicts to avoid repeat notifications
                new_sw_name = added_entries_shared[host]
                old_sw_name = removed_entries_shared[host]
                removed_entries_shared.pop(host)
                # Start a new thread to handle bounce entry creation and removal
                bounce_entry_thread = threading.Thread(target=handleBounceEntry, args=(p4info_helper, host, new_sw_name, old_sw_name, switches))
                bounce_entry_thread.start()
            added_entries_shared.pop(host)

        time.sleep(1)  # pause for a reasonable duration```

def get_ipv4_dst_address(entry):
    for m in entry.match:
        if m.field_id == 1:  # Assuming that the field_id for ipv4.dstAddr is 1
            return m.lpm.value  # Return the IP address as integer
    return None

def addHostOnSwitch(p4info_helper, switch, host_ip, host_mac):
    channel = grpc.insecure_channel(switch.address)
    stub = p4runtime_pb2_grpc.P4RuntimeStub(channel)
    request = p4runtime_pb2.WriteRequest()
    request.device_id = switch.device_id
    request.election_id.low = 1
    update = request.updates.add()
    update.type = p4runtime_pb2.Update.MODIFY
    table_entry = p4info_helper.buildTableEntry(
        table_name="MyIngress.ipv4_lpm",
        match_fields={
            "hdr.ipv4.dstAddr": (host_ip, 32)
        },
        action_name="MyIngress.ipv4_forward",
        action_params={
            "dstAddr": host_mac,
            "port": 4
        })
    update.entity.table_entry.CopyFrom(table_entry)
    stub.Write(request)

def removeHostFromSwitch(switch, host_ip):
    channel = grpc.insecure_channel(switch.address)
    stub = p4runtime_pb2_grpc.P4RuntimeStub(channel)
    request = p4runtime_pb2.WriteRequest()
    request.device_id = switch.device_id
    request.election_id.low = 1
    update = request.updates.add()
    update.type = p4runtime_pb2.Update.DELETE
    entry = None
    for resp in switch.ReadTableEntries():
        for entity in resp.entities:
            for m in entity.table_entry.match:
                if m.field_id == 1 and socket.inet_ntoa(m.lpm.value) == host_ip:
                    entry = entity.table_entry
                    break
            if entry is not None:
                break
        if entry is not None:
            break

    update.entity.table_entry.CopyFrom(entry)
    stub.Write(request)

def main(p4info_file_path, bmv2_file_path):
    # Instantiate a P4Runtime helper from the p4info file
    p4info_helper = p4runtime_lib.helper.P4InfoHelper(p4info_file_path)

    try:
        # Create a switch connection object for s1 and s2;
        # this is backed by a P4Runtime gRPC connection.
        # Also, dump all P4Runtime messages sent to switch to given txt files.
        s1 = p4runtime_lib.bmv2.Bmv2SwitchConnection(
            name='s1',
            address='127.0.0.1:50051',
            device_id=0,
            proto_dump_file='logs/s1-p4runtime-requests.txt')
        s2 = p4runtime_lib.bmv2.Bmv2SwitchConnection(
            name='s2',
            address='127.0.0.1:50052',
            device_id=1,
            proto_dump_file='logs/s2-p4runtime-requests.txt')
        s3 = p4runtime_lib.bmv2.Bmv2SwitchConnection(
            name='s3',
            address='127.0.0.1:50053',
            device_id=2,
            proto_dump_file='logs/s3-p4runtime-requests.txt')
        s4 = p4runtime_lib.bmv2.Bmv2SwitchConnection(
            name='s4',
            address='127.0.0.1:50054',
            device_id=3,
            proto_dump_file='logs/s4-p4runtime-requests.txt')

        # Send master arbitration update message to establish this controller as
        # master (required by P4Runtime before performing any other write operation)
        s1.MasterArbitrationUpdate()
        s2.MasterArbitrationUpdate()
        s3.MasterArbitrationUpdate()
        s4.MasterArbitrationUpdate()

        # Create shared memory dictionaries for added and removed entries
        added_entries_shared = {}
        removed_entries_shared = {}

        # Create and start a separate thread to monitor each switch
        monitor_s1 = Thread(target=monitorHostLocation, args=(s1, added_entries_shared, removed_entries_shared))
        monitor_s2 = Thread(target=monitorHostLocation, args=(s2, added_entries_shared, removed_entries_shared))

        monitor_s1.start()
        monitor_s2.start()

        # Create and start a separate thread for checking migration
        monitor_migration = Thread(target=checkForMigration, args=(p4info_helper, added_entries_shared, removed_entries_shared, [s1, s2, s3, s4]))
        monitor_migration.start()

        # Perform migration between s2 and s1
        sleep(5)
        removeHostFromSwitch(s2, "10.0.2.2")
        addHostOnSwitch(p4info_helper, s1, "10.0.2.2", "08:00:00:00:02:22")

    except KeyboardInterrupt:
        print(" Shutting down.")
        # Join the threads
        monitor_s1.join()
        monitor_s2.join()
        monitor_migration.join()
    except grpc.RpcError as e:
        printGrpcError(e)

    ShutdownAllSwitchConnections()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='P4Runtime Controller')
    parser.add_argument('--p4info', help='p4info proto in text format from p4c',
                        type=str, action="store", required=False,
                        default='./build/advanced_tunnel.p4.p4info.txt')
    parser.add_argument('--bmv2-json', help='BMv2 JSON file from p4c',
                        type=str, action="store", required=False,
                        default='./build/advanced_tunnel.json')
    args = parser.parse_args()

    if not os.path.exists(args.p4info):
        parser.print_help()
        print("\np4info file not found: %s\nHave you run 'make'?" % args.p4info)
        parser.exit(1)
    if not os.path.exists(args.bmv2_json):
        parser.print_help()
        print("\nBMv2 JSON file not found: %s\nHave you run 'make'?" % args.bmv2_json)
        parser.exit(1)
    main(args.p4info, args.bmv2_json)

