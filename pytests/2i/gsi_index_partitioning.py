import copy
import json
import threading

from base_2i import BaseSecondaryIndexingTests
from membase.api.rest_client import RestConnection, RestHelper
import random
from lib import testconstants
from lib.memcached.helper.data_helper import MemcachedClientHelper
from lib.remote.remote_util import RemoteMachineShellConnection
from threading import Thread
from pytests.query_tests_helper import QueryHelperTests
from couchbase_helper.documentgenerator import JsonDocGenerator
from couchbase_helper.cluster import Cluster
from gsi_replica_indexes import GSIReplicaIndexesTests


class GSIIndexPartitioningTests(GSIReplicaIndexesTests):
    def setUp(self):
        super(GSIIndexPartitioningTests, self).setUp()
        self.index_servers = self.get_nodes_from_services_map(
            service_type="index", get_all_nodes=True)
        self.rest = RestConnection(self.index_servers[0])
        self.node_list = []
        for server in self.index_servers:
            self.node_list.append(server.ip + ":" + server.port)

        self.num_queries = self.input.param("num_queries", 100)
        self.num_index_partitions = self.input.param("num_index_partitions", 16)
        self.recover_failed_node = self.input.param("recover_failed_node",
                                                    False)
        self.op_type = self.input.param("op_type", "create")

    def tearDown(self):
        super(GSIIndexPartitioningTests, self).tearDown()

    # Test that generates n number of create index statements with various permutations and combinations
    # of different clauses used in the create index statement.
    def test_create_partitioned_indexes(self):
        self._load_emp_dataset(end=self.num_items)

        create_index_queries = self.generate_random_create_index_statements(
            bucketname=self.buckets[0].name, idx_node_list=self.node_list,
            num_statements=self.num_queries)

        failed_index_creation = 0
        for create_index_query in create_index_queries:

            try:
                self.n1ql_helper.run_cbq_query(
                    query=create_index_query["index_definition"],
                    server=self.n1ql_node)
            except Exception, ex:
                self.log.info(str(ex))

            self.sleep(10)

            index_metadata = self.rest.get_indexer_metadata()

            self.log.info("output from /getIndexStatus")
            self.log.info(index_metadata)

            self.log.info("Index Map")
            index_map = self.get_index_map()
            self.log.info(index_map)

            if index_metadata:
                status = self.validate_partitioned_indexes(create_index_query,
                                                           index_map,
                                                           index_metadata)

                if not status:
                    failed_index_creation += 1
                    self.log.info(
                        "** Following query failed validation : {0}".format(
                            create_index_query["index_definition"]))
            else:
                failed_index_creation += 1
                self.log.info(
                    "** Following index did not get created : {0}".format(
                        create_index_query["index_definition"]))

            drop_index_query = "DROP INDEX default.{0}".format(
                create_index_query["index_name"])
            try:
                self.n1ql_helper.run_cbq_query(
                    query=drop_index_query,
                    server=self.n1ql_node)
            except Exception, ex:
                self.log.info(str(ex))

            self.sleep(5)

        self.log.info(
            "Total Create Index Statements Run: {0}, Passed : {1}, Failed : {2}".format(
                self.num_queries, self.num_queries - failed_index_creation,
                failed_index_creation))
        self.assertTrue(failed_index_creation == 0,
                        "Some create index statements failed validations. Pls see the test log above for details.")

    def test_partition_index_with_excluded_nodes(self):
        self._load_emp_dataset(end=self.num_items)

        # Setting to exclude a node for planner
        self.rest.set_index_planner_settings("excludeNode=in")
        # Create partitioned index
        create_index_statement = "CREATE INDEX idx1 on default(name,dept,salary) partition by hash(name)"

        try:
            self.n1ql_helper.run_cbq_query(
                query=create_index_statement,
                server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))

        # Validate index created and check the hosts on which partitions are hosted.
        expected_hosts = self.node_list[1:]
        expected_hosts.sort()
        validated = False
        index_metadata = self.rest.get_indexer_metadata()
        self.log.info("Indexer Metadata :::")
        self.log.info(index_metadata)

        for index in index_metadata["status"]:
            if index["name"] == "idx1":
                self.log.info("Expected Hosts : {0}".format(expected_hosts))
                self.log.info("Actual Hosts   : {0}".format(index["hosts"]))
                self.assertEqual(index["hosts"], expected_hosts,
                                 "Planner did not ignore excluded node during index creation")
                validated = True

        if not validated:
            self.fail("Looks like index was not created.")

    def test_partitioned_index_with_replica(self):
        self._load_emp_dataset(end=self.num_items)

        # Create partitioned index
        create_index_statement = "CREATE INDEX idx1 on default(name,dept,salary) partition by hash(name) with {{'num_replica':{0}, 'num_partition':{1}}}".format(
            self.num_index_replicas, self.num_index_partitions)

        try:
            self.n1ql_helper.run_cbq_query(
                query=create_index_statement,
                server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))

        index_metadata = self.rest.get_indexer_metadata()
        self.log.info("Indexer Metadata :::")
        self.log.info(index_metadata)

        self.assertTrue(self.validate_partition_map(index_metadata, "idx1",
                                                    self.num_index_replicas,
                                                    self.num_index_partitions),
                        "Partition map validation failed")

    def test_partitioned_index_with_replica_with_server_groups(self):
        self._load_emp_dataset(end=self.num_items)
        self._create_server_groups()

        # Create partitioned index
        create_index_statement = "CREATE INDEX idx1 on default(name,dept,salary) partition by hash(name) with {{'num_replica':{0}}}".format(
            self.num_index_replicas)

        try:
            self.n1ql_helper.run_cbq_query(
                query=create_index_statement,
                server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))

        index_metadata = self.rest.get_indexer_metadata()

        index_hosts_list = []
        for index in index_metadata["status"]:
            index_hosts_list.append(index["hosts"])

        self.log.info("Index Host List : {0}".format(index_hosts_list))

        # Need to change the validation logic here. Between index and its replicas, they should have a full set of partitions in both the server groups.
        # idx11 - .101, .102: 3, 4, 5, 10, 11, 15, 16
        # idx11 - .103, .104: 1, 2, 6, 7, 8, 9, 12, 13, 14

        # idx12 - .101, .102: 1, 2, 6, 7, 8, 9, 12, 13, 14
        # idx12 - .103, .104: 3, 4, 5, 10, 11, 15, 16

        validation = True
        for i in range(0, len(index_hosts_list)):
            for j in range(i + 1, len(index_hosts_list)):
                if (index_hosts_list[i].sort() != index_hosts_list[j].sort()):
                    continue
                else:
                    validation &= False

        self.assertTrue(validation,
                        "Partitions of replica indexes do not honour server grouping")

    def test_create_partitioned_index_one_node_already_down(self):
        self._load_emp_dataset(end=self.num_items)

        node_out = self.servers[self.node_out]
        failover_task = self.cluster.async_failover(
            self.servers[:self.nodes_init],
            [node_out],
            self.graceful, wait_for_pending=60)

        failover_task.result()

        # Create partitioned index
        create_index_statement = "CREATE INDEX idx1 on default(name,dept,salary) partition by hash(name)"

        try:
            self.n1ql_helper.run_cbq_query(
                query=create_index_statement,
                server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))
            self.fail("Failed to create index with one node failed")

        index_metadata = self.rest.get_indexer_metadata()
        self.log.info("Indexer Metadata :::")
        self.log.info(index_metadata)

        hosts = index_metadata["status"][0]["hosts"]
        self.log.info("Actual nodes : {0}".format(hosts))
        node_out_str = node_out.ip + ":" + node_out.port
        self.assertTrue(node_out_str not in hosts,
                        "Partitioned index not created on expected hosts")

    def test_create_partitioned_index_one_node_network_partitioned(self):
        self._load_emp_dataset(end=self.num_items)

        node_out = self.servers[self.node_out]
        self.start_firewall_on_node(node_out)

        # Create partitioned index
        create_index_statement = "CREATE INDEX idx1 on default(name,dept,salary) partition by hash(name)"

        try:
            self.n1ql_helper.run_cbq_query(
                query=create_index_statement,
                server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))
            self.fail("Failed to create index with one node failed")

        index_metadata = self.rest.get_indexer_metadata()
        self.log.info("Indexer Metadata :::")
        self.log.info(index_metadata)

        self.stop_firewall_on_node(node_out)

        hosts = index_metadata["status"][0]["hosts"]
        self.log.info("Actual nodes : {0}".format(hosts))
        node_out_str = node_out.ip + ":" + node_out.port
        self.assertTrue(node_out_str not in hosts,
                        "Partitioned index not created on expected hosts")

    def test_node_fails_during_create_partitioned_index(self):
        self._load_emp_dataset(end=self.num_items)

        node_out = self.servers[self.node_out]

        # Create partitioned index
        create_index_statement = "CREATE INDEX idx1 on default(name,dept,salary) partition by hash(name)"

        threads = []
        threads.append(
            Thread(target=self.n1ql_helper.run_cbq_query, name="run_query",
                   args=(create_index_statement, 10, self.n1ql_node)))
        threads.append(
            Thread(target=self.cluster.failover, name="failover", args=(
                self.servers[:self.nodes_init], [node_out], self.graceful,
                False, 60)))

        for thread in threads:
            thread.start()
        self.sleep(5)
        for thread in threads:
            thread.join()

        self.sleep(30)

        index_metadata = self.rest.get_indexer_metadata()
        self.log.info("Indexer Metadata :::")
        self.log.info(index_metadata)

    def test_node_nw_partitioned_during_create_partitioned_index(self):
        self._load_emp_dataset(end=self.num_items)

        node_out = self.servers[self.node_out]

        # Create partitioned index
        create_index_statement = "CREATE INDEX idx1 on default(name,dept,salary) partition by hash(name)"

        threads = []
        threads.append(
            Thread(target=self.start_firewall_on_node,
                   name="network_partitioning", args=(node_out,)))
        threads.append(
            Thread(target=self.n1ql_helper.run_cbq_query, name="run_query",
                   args=(create_index_statement, 10, self.n1ql_node)))

        for thread in threads:
            thread.start()
        self.sleep(5)
        for thread in threads:
            thread.join()

        self.sleep(10)

        try:
            index_metadata = self.rest.get_indexer_metadata()
            self.log.info("Indexer Metadata :::")
            self.log.info(index_metadata)
            if index_metadata != {}:
                hosts = index_metadata["status"][0]["hosts"]
                self.log.info("Actual nodes : {0}".format(hosts))
                node_out_str = node_out.ip + ":" + node_out.port
                self.assertTrue(node_out_str not in hosts,
                                "Partitioned index not created on expected hosts")
            else:
                self.log.info(
                    "Cannot retrieve index metadata since one node is down")
        except Exception, ex:
            self.log.info(str(ex))
        finally:
            self.stop_firewall_on_node(node_out)
            self.sleep(30)
            index_metadata = self.rest.get_indexer_metadata()
            self.log.info("Indexer Metadata :::")
            self.log.info(index_metadata)

            hosts = index_metadata["status"][0]["hosts"]
            node_out_str = node_out.ip + ":" + node_out.port
            self.assertTrue(node_out_str in hosts,
                            "Partitioned index not created on all hosts")

    def test_node_nw_partitioned_during_create_partitioned_index_with_node_list(
            self):
        self._load_emp_dataset(end=self.num_items)

        node_out = self.servers[self.node_out]
        node_list_str = "[\"" + "\",\"".join(self.node_list) + "\"]"

        # Create partitioned index
        create_index_statement = "CREATE INDEX idx1 on default(name,dept,salary) partition by hash(name) with {{'nodes' : {0}}}".format(
            node_list_str)

        threads = []

        threads.append(
            Thread(target=self.start_firewall_on_node,
                   name="network_partitioning", args=(node_out,)))
        threads.append(
            Thread(target=self.n1ql_helper.run_cbq_query, name="run_query",
                   args=(create_index_statement, 10, self.n1ql_node)))

        for thread in threads:
            thread.start()
        self.sleep(5)
        for thread in threads:
            thread.join()

        self.sleep(10)

        try:
            index_metadata = self.rest.get_indexer_metadata()
            self.log.info("Indexer Metadata :::")
            self.log.info(index_metadata)
            if index_metadata != {}:
                hosts = index_metadata["status"][0]["hosts"]
                self.log.info("Actual nodes : {0}".format(hosts))
                node_out_str = node_out.ip + ":" + node_out.port
                self.assertTrue(node_out_str not in hosts,
                                "Partitioned index not created on expected hosts")
            else:
                self.log.info(
                    "Cannot retrieve index metadata since one node is down")
        except Exception, ex:
            self.log.info(str(ex))
        finally:
            self.stop_firewall_on_node(node_out)
            self.sleep(30)
            index_metadata = self.rest.get_indexer_metadata()
            self.log.info("Indexer Metadata :::")
            self.log.info(index_metadata)

            hosts = index_metadata["status"][0]["hosts"]
            node_out_str = node_out.ip + ":" + node_out.port
            self.assertTrue(node_out_str in hosts,
                            "Partitioned index not created on all hosts")

    def test_build_partitioned_index(self):
        self._load_emp_dataset(end=self.num_items)

        index_name_prefix = "random_index_" + str(
            random.randint(100000, 999999))
        if self.num_index_replicas > 0:
            create_index_query = "CREATE INDEX " + index_name_prefix + " ON default(name,dept,salary) partition by hash(name) USING GSI  WITH {{'num_partition': {0}, 'defer_build': true, 'num_replica':{1}}};".format(
                self.num_index_partitions, self.num_index_replicas)
        else:
            create_index_query = "CREATE INDEX " + index_name_prefix + " ON default(name,dept,salary) partition by hash(name) USING GSI  WITH {{'num_partition': {0}, 'defer_build': true}};".format(
                self.num_index_partitions)
        try:
            self.n1ql_helper.run_cbq_query(query=create_index_query,
                                           server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))
            self.fail("index creation failed with error : {0}".format(str(ex)))

        self.sleep(10)
        index_map = self.get_index_map()
        self.log.info(index_map)

        index_metadata = self.rest.get_indexer_metadata()
        self.log.info("Indexer Metadata Before Build:")
        self.log.info(index_metadata)

        index_details = {}
        index_details["index_name"] = index_name_prefix
        index_details["num_partitions"] = self.num_index_partitions
        index_details["defer_build"] = True

        self.assertTrue(
            self.validate_partitioned_indexes(index_details, index_map,
                                              index_metadata),
            "Deferred Partitioned index created not as expected")

        # Validation for replica indexes
        if self.num_index_replicas > 0:
            for i in range(1, self.num_index_replicas + 1):
                index_details[
                    "index_name"] = index_name_prefix + " (replica {0})".format(
                    str(i))
                self.assertTrue(
                    self.validate_partitioned_indexes(index_details, index_map,
                                                      index_metadata),
                    "Deferred Partitioned index created not as expected")

        build_index_query = "BUILD INDEX on `default`(" + index_name_prefix + ")"

        try:
            self.n1ql_helper.run_cbq_query(query=build_index_query,
                                           server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))
            self.fail("index building failed with error : {0}".format(str(ex)))

        self.sleep(30)
        index_map = self.get_index_map()
        index_metadata = self.rest.get_indexer_metadata()
        self.log.info("Indexer Metadata After Build:")
        self.log.info(index_metadata)

        index_details["index_name"] = index_name_prefix
        index_details["defer_build"] = False

        self.assertTrue(
            self.validate_partitioned_indexes(index_details, index_map,
                                              index_metadata),
            "Deferred Partitioned index created not as expected")
        # Validation for replica indexes
        if self.num_index_replicas > 0:
            for i in range(1, self.num_index_replicas + 1):
                index_details[
                    "index_name"] = index_name_prefix + " (replica {0})".format(
                    str(i))
                self.assertTrue(
                    self.validate_partitioned_indexes(index_details, index_map,
                                                      index_metadata),
                    "Deferred Partitioned index created not as expected")

    def test_build_partitioned_index_one_failed_node(self):
        self._load_emp_dataset(end=self.num_items)

        index_name_prefix = "random_index_" + str(
            random.randint(100000, 999999))
        node_list_str = "[\"" + "\",\"".join(self.node_list) + "\"]"
        create_index_query = "CREATE INDEX " + index_name_prefix + " ON default(name,dept,salary) partition by hash(name) USING GSI  WITH {{'num_partition': {0}, 'nodes': {1}, 'defer_build': true}};".format(
            self.num_index_partitions, node_list_str)
        try:
            self.n1ql_helper.run_cbq_query(query=create_index_query,
                                           server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))
            self.fail("index creation failed with error : {0}".format(str(ex)))

        self.sleep(10)
        index_map = self.get_index_map()
        self.log.info(index_map)

        index_metadata = self.rest.get_indexer_metadata()
        self.log.info("Indexer Metadata Before Build:")
        self.log.info(index_metadata)

        index_details = {}
        index_details["index_name"] = index_name_prefix
        index_details["num_partitions"] = self.num_index_partitions
        index_details["defer_build"] = True

        self.assertTrue(
            self.validate_partitioned_indexes(index_details, index_map,
                                              index_metadata),
            "Deferred Partitioned index created not as expected")

        node_out = self.servers[self.node_out]
        failover_task = self.cluster.async_failover(
            self.servers[:self.nodes_init],
            [node_out],
            self.graceful, wait_for_pending=180)

        failover_task.result()

        build_index_query = "BUILD INDEX on `default`(" + index_name_prefix + ")"

        try:
            self.n1ql_helper.run_cbq_query(query=build_index_query,
                                           server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))
            self.fail("index building failed with error : {0}".format(str(ex)))

        self.sleep(30)
        index_map = self.get_index_map()
        index_metadata = self.rest.get_indexer_metadata()
        self.log.info("Indexer Metadata After Build:")
        self.log.info(index_metadata)

        index_details["defer_build"] = False

        self.assertTrue(
            self.validate_partitioned_indexes(index_details, index_map,
                                              index_metadata),
            "Deferred Partitioned index created not as expected")

        if self.recover_failed_node:
            nodes_all = self.rest.node_statuses()
            for node in nodes_all:
                if node.ip == node_out.ip:
                    break

            self.rest.set_recovery_type(node.id, self.recovery_type)
            self.rest.add_back_node(node.id)

            rebalance = self.cluster.async_rebalance(
                self.servers[:self.nodes_init],
                [], [])
            reached = RestHelper(self.rest).rebalance_reached()
            self.assertTrue(reached,
                            "rebalance failed, stuck or did not complete")
            rebalance.result()
            self.sleep(180)

            index_map = self.get_index_map()
            index_metadata = self.rest.get_indexer_metadata()
            self.log.info("Indexer Metadata After Build:")
            self.log.info(index_metadata)

            index_details["defer_build"] = False

            self.assertTrue(
                self.validate_partitioned_indexes(index_details, index_map,
                                                  index_metadata),
                "Deferred Partitioned index created not as expected")

    def test_failover_during_build_partitioned_index(self):
        self._load_emp_dataset(end=self.num_items)

        index_name_prefix = "random_index_" + str(
            random.randint(100000, 999999))
        node_list_str = "[\"" + "\",\"".join(self.node_list) + "\"]"
        create_index_query = "CREATE INDEX " + index_name_prefix + " ON default(name,dept,salary) partition by hash(name) USING GSI  WITH {{'num_partition': {0}, 'nodes': {1}, 'defer_build': true}};".format(
            self.num_index_partitions, node_list_str)
        try:
            self.n1ql_helper.run_cbq_query(query=create_index_query,
                                           server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))
            self.fail("index creation failed with error : {0}".format(str(ex)))

        self.sleep(10)
        index_map = self.get_index_map()
        self.log.info(index_map)

        index_metadata = self.rest.get_indexer_metadata()
        self.log.info("Indexer Metadata Before Build:")
        self.log.info(index_metadata)

        index_details = {}
        index_details["index_name"] = index_name_prefix
        index_details["num_partitions"] = self.num_index_partitions
        index_details["defer_build"] = True

        self.assertTrue(
            self.validate_partitioned_indexes(index_details, index_map,
                                              index_metadata),
            "Deferred Partitioned index created not as expected")

        node_out = self.servers[self.node_out]
        build_index_query = "BUILD INDEX on `default`(" + index_name_prefix + ")"
        threads = []
        threads.append(
            Thread(target=self.n1ql_helper.run_cbq_query, name="run_query",
                   args=(build_index_query, 10, self.n1ql_node)))
        threads.append(
            Thread(target=self.cluster.async_failover, name="failover", args=(
                self.servers[:self.nodes_init], [node_out], self.graceful)))
        for thread in threads:
            thread.start()
            thread.join()
        self.sleep(30)

        index_map = self.get_index_map()
        index_metadata = self.rest.get_indexer_metadata()
        self.log.info("Indexer Metadata After Build:")
        self.log.info(index_metadata)

        index_details["defer_build"] = False

        self.assertTrue(
            self.validate_partitioned_indexes(index_details, index_map,
                                              index_metadata),
            "Deferred Partitioned index created not as expected")

    def test_build_partitioned_index_with_network_partitioning(self):
        self._load_emp_dataset(end=self.num_items)

        index_name_prefix = "random_index_" + str(
            random.randint(100000, 999999))
        node_list_str = "[\"" + "\",\"".join(self.node_list) + "\"]"
        create_index_query = "CREATE INDEX " + index_name_prefix + " ON default(name,dept,salary) partition by hash(name) USING GSI  WITH {{'num_partition': {0}, 'nodes': {1}, 'defer_build': true}};".format(
            self.num_index_partitions, node_list_str)
        try:
            self.n1ql_helper.run_cbq_query(query=create_index_query,
                                           server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))
            self.fail("index creation failed with error : {0}".format(str(ex)))

        self.sleep(10)
        index_map = self.get_index_map()
        self.log.info(index_map)

        index_metadata = self.rest.get_indexer_metadata()
        self.log.info("Indexer Metadata Before Build:")
        self.log.info(index_metadata)

        index_details = {}
        index_details["index_name"] = index_name_prefix
        index_details["num_partitions"] = self.num_index_partitions
        index_details["defer_build"] = True

        self.assertTrue(
            self.validate_partitioned_indexes(index_details, index_map,
                                              index_metadata),
            "Deferred Partitioned index created not as expected")

        node_out = self.servers[self.node_out]
        failover_task = self.cluster.async_failover(
            self.servers[:self.nodes_init],
            [node_out],
            self.graceful, wait_for_pending=180)

        failover_task.result()

        build_index_query = "BUILD INDEX on `default`(" + index_name_prefix + ")"

        try:
            self.start_firewall_on_node(node_out)
            self.sleep(10)
            self.n1ql_helper.run_cbq_query(query=build_index_query,
                                           server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))
            self.fail("index building failed with error : {0}".format(str(ex)))

        finally:
            # Heal network partition and wait for some time to allow indexes
            # to get built automatically on that node
            self.stop_firewall_on_node(node_out)
            self.sleep(360)

            index_map = self.get_index_map()
            index_metadata = self.rest.get_indexer_metadata()
            self.log.info("Indexer Metadata After Build:")
            self.log.info(index_metadata)

            index_details["defer_build"] = False

            self.assertTrue(
                self.validate_partitioned_indexes(index_details, index_map,
                                                  index_metadata),
                "Deferred Partitioned index created not as expected")

    def test_drop_partitioned_index(self):
        self._load_emp_dataset(end=self.num_items)

        index_name_prefix = "random_index_" + str(
            random.randint(100000, 999999))

        with_clause = "WITH {{'num_partition': {0} ".format(
            self.num_index_partitions)
        if self.num_index_replicas > 0:
            with_clause += ", 'num_replica':{0}".format(self.num_index_replicas)
        if self.defer_build:
            with_clause += ", 'defer_build':True"
        with_clause += " }"

        create_index_query = "CREATE INDEX " + index_name_prefix + " ON default(name,dept,salary) partition by hash(name) USING GSI  {0}".format(
            with_clause)

        try:
            self.n1ql_helper.run_cbq_query(query=create_index_query,
                                           server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))
            self.fail(
                "index creation failed with error : {0}".format(str(ex)))

        self.sleep(10)
        index_map = self.get_index_map()
        self.log.info(index_map)

        index_metadata = self.rest.get_indexer_metadata()
        self.log.info("Indexer Metadata Before Build:")
        self.log.info(index_metadata)

        index_details = {}
        index_details["index_name"] = index_name_prefix
        index_details["num_partitions"] = self.num_index_partitions
        index_details["defer_build"] = self.defer_build

        self.assertTrue(
            self.validate_partitioned_indexes(index_details, index_map,
                                              index_metadata),
            "Deferred Partitioned index created not as expected")

        # Validation for replica indexes
        if self.num_index_replicas > 0:
            for i in range(1, self.num_index_replicas + 1):
                index_details[
                    "index_name"] = index_name_prefix + " (replica {0})".format(
                    str(i))
                self.assertTrue(
                    self.validate_partitioned_indexes(index_details,
                                                      index_map,
                                                      index_metadata),
                    "Deferred Partitioned index created not as expected")

        drop_index_query = "DROP INDEX `default`." + index_name_prefix

        try:
            self.n1ql_helper.run_cbq_query(query=drop_index_query,
                                           server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))
            self.fail(
                "Drop index failed with error : {0}".format(str(ex)))

        self.sleep(30)
        index_map = self.get_index_map()
        self.log.info("Index map after drop index: %s", index_map)
        if not index_map == {}:
            self.fail("Indexes not dropped correctly")

    def test_drop_partitioned_index_one_failed_node(self):
        self._load_emp_dataset(end=self.num_items)

        index_name_prefix = "random_index_" + str(
            random.randint(100000, 999999))
        node_list_str = "[\"" + "\",\"".join(self.node_list) + "\"]"
        create_index_query = "CREATE INDEX " + index_name_prefix + " ON default(name,dept,salary) partition by hash(name) USING GSI  WITH {{'num_partition': {0}, 'nodes': {1}}};".format(
            self.num_index_partitions, node_list_str)
        try:
            self.n1ql_helper.run_cbq_query(query=create_index_query,
                                           server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))
            self.fail(
                "index creation failed with error : {0}".format(str(ex)))

        self.sleep(10)
        index_map = self.get_index_map()
        self.log.info(index_map)

        index_metadata = self.rest.get_indexer_metadata()
        self.log.info("Indexer Metadata:")
        self.log.info(index_metadata)

        index_details = {}
        index_details["index_name"] = index_name_prefix
        index_details["num_partitions"] = self.num_index_partitions
        index_details["defer_build"] = False

        self.assertTrue(
            self.validate_partitioned_indexes(index_details, index_map,
                                              index_metadata),
            "Partitioned index created not as expected")

        node_out = self.servers[self.node_out]
        failover_task = self.cluster.async_failover(
            self.servers[:self.nodes_init],
            [node_out],
            self.graceful, wait_for_pending=180)

        failover_task.result()

        drop_index_query = "DROP INDEX `default`." + index_name_prefix

        try:
            self.n1ql_helper.run_cbq_query(query=drop_index_query,
                                           server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))
            self.fail(
                "Drop index failed with error : {0}".format(str(ex)))

        self.sleep(30)
        index_map = self.get_index_map()
        self.log.info("Index map after drop index: %s", index_map)
        if not index_map == {}:
            self.fail("Indexes not dropped correctly")

        if self.recover_failed_node:
            nodes_all = self.rest.node_statuses()
            for node in nodes_all:
                if node.ip == node_out.ip:
                    break

            self.rest.set_recovery_type(node.id, self.recovery_type)
            self.rest.add_back_node(node.id)

            rebalance = self.cluster.async_rebalance(
                self.servers[:self.nodes_init],
                [], [])
            reached = RestHelper(self.rest).rebalance_reached()
            self.assertTrue(reached,
                            "rebalance failed, stuck or did not complete")
            rebalance.result()
            self.sleep(180)

            index_map = self.get_index_map()
            self.log.info("Index map after drop index: %s", index_map)
            if not index_map == {}:
                self.fail("Indexes not dropped correctly")

    def test_failover_during_drop_partitioned_index(self):
        self._load_emp_dataset(end=self.num_items)

        index_name_prefix = "random_index_" + str(
            random.randint(100000, 999999))
        node_list_str = "[\"" + "\",\"".join(self.node_list) + "\"]"
        create_index_query = "CREATE INDEX " + index_name_prefix + " ON default(name,dept,salary) partition by hash(name) USING GSI  WITH {{'num_partition': {0}, 'nodes': {1}}};".format(
            self.num_index_partitions, node_list_str)
        try:
            self.n1ql_helper.run_cbq_query(query=create_index_query,
                                           server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))
            self.fail("index creation failed with error : {0}".format(
                str(ex)))

        self.sleep(10)
        index_map = self.get_index_map()
        self.log.info(index_map)

        index_metadata = self.rest.get_indexer_metadata()
        self.log.info("Indexer Metadata:")
        self.log.info(index_metadata)

        index_details = {}
        index_details["index_name"] = index_name_prefix
        index_details["num_partitions"] = self.num_index_partitions
        index_details["defer_build"] = False

        self.assertTrue(
            self.validate_partitioned_indexes(index_details, index_map,
                                              index_metadata),
            "Partitioned index created not as expected")

        node_out = self.servers[self.node_out]
        drop_index_query = "DROP INDEX `default`." + index_name_prefix
        threads = []
        threads.append(
            Thread(target=self.n1ql_helper.run_cbq_query,
                   name="run_query",
                   args=(drop_index_query, 10, self.n1ql_node)))
        threads.append(
            Thread(target=self.cluster.async_failover, name="failover",
                   args=(
                       self.servers[:self.nodes_init], [node_out],
                       self.graceful)))
        for thread in threads:
            thread.start()
            thread.join()
        self.sleep(30)

        index_map = self.get_index_map()
        self.log.info("Index map after drop index: %s", index_map)
        if not index_map == {}:
            self.fail("Indexes not dropped correctly")

    def test_drop_partitioned_index_with_network_partitioning(self):
        self._load_emp_dataset(end=self.num_items)

        index_name_prefix = "random_index_" + str(
            random.randint(100000, 999999))
        node_list_str = "[\"" + "\",\"".join(self.node_list) + "\"]"
        create_index_query = "CREATE INDEX " + index_name_prefix + " ON default(name,dept,salary) partition by hash(name) USING GSI  WITH {{'num_partition': {0}, 'nodes': {1}}};".format(
            self.num_index_partitions, node_list_str)
        try:
            self.n1ql_helper.run_cbq_query(query=create_index_query,
                                           server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))
            self.fail(
                "index creation failed with error : {0}".format(str(ex)))

        self.sleep(10)
        index_map = self.get_index_map()
        self.log.info(index_map)

        index_metadata = self.rest.get_indexer_metadata()
        self.log.info("Indexer Metadata Before Build:")
        self.log.info(index_metadata)

        index_details = {}
        index_details["index_name"] = index_name_prefix
        index_details["num_partitions"] = self.num_index_partitions
        index_details["defer_build"] = False

        self.assertTrue(
            self.validate_partitioned_indexes(index_details, index_map,
                                              index_metadata),
            "Partitioned index created not as expected")

        node_out = self.servers[self.node_out]
        failover_task = self.cluster.async_failover(
            self.servers[:self.nodes_init],
            [node_out],
            self.graceful, wait_for_pending=180)

        failover_task.result()

        drop_index_query = "DROP INDEX `default`." + index_name_prefix

        try:
            self.start_firewall_on_node(node_out)
            self.sleep(10)
            self.n1ql_helper.run_cbq_query(query=drop_index_query,
                                           server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))
            self.fail(
                "index drop failed with error : {0}".format(str(ex)))

        finally:
            # Heal network partition and wait for some time to allow indexes
            # to get built automatically on that node
            self.stop_firewall_on_node(node_out)
            self.sleep(360)

            index_map = self.get_index_map()
            self.log.info("Index map after drop index: %s", index_map)
            if not index_map == {}:
                self.fail("Indexes not dropped correctly")

    def test_mutations_on_partitioned_indexes(self):
        self.run_async_index_operations(operation_type="create_index")
        self.run_doc_ops()
        self.sleep(30)

        # Get item counts
        bucket_item_count, total_item_count, total_num_docs_processed = self.get_stats_for_partitioned_indexes()

        self.assertEqual(bucket_item_count, total_item_count,
                         "# Items indexed {0} do not match bucket items {1}".format(
                             total_item_count, bucket_item_count))

    def test_update_mutations_on_indexed_keys_partitioned_indexes(self):
        create_index_query = "CREATE INDEX idx1 ON default(name,mutated) partition by hash(name) USING GSI;"
        try:
            self.n1ql_helper.run_cbq_query(query=create_index_query,
                                           server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))
            self.fail(
                "index creation failed with error : {0}".format(str(ex)))
        self.run_doc_ops()
        self.sleep(30)

        # Get item counts
        bucket_item_count, total_item_count, total_num_docs_processed = self.get_stats_for_partitioned_indexes(
            index_name="idx1")

        self.assertEqual(bucket_item_count, total_item_count,
                         "# Items indexed {0} do not match bucket items {1}".format(
                             total_item_count, bucket_item_count))

    def test_kv_full_rollback_on_partitioned_indexes(self):
        self.run_async_index_operations(operation_type="create_index")
        self.sleep(30)

        self.cluster.bucket_flush(self.master)
        self.sleep(60)

        # Get item counts
        bucket_item_count, total_item_count, total_num_docs_processed = self.get_stats_for_partitioned_indexes()

        self.assertEqual(total_item_count, 0, "Rollback to zero fails")

    def test_kv_partial_rollback_on_partitioned_indexes(self):
        self.run_async_index_operations(operation_type="create_index")

        # Stop Persistence on Node A & Node B
        self.log.info("Stopping persistence on NodeA & NodeB")
        mem_client = MemcachedClientHelper.direct_client(self.servers[0],
                                                         "default")
        mem_client.stop_persistence()
        mem_client = MemcachedClientHelper.direct_client(self.servers[1],
                                                         "default")
        mem_client.stop_persistence()

        self.run_doc_ops()

        self.sleep(10)

        # Get count before rollback
        bucket_count_before_rollback, item_count_before_rollback, num_docs_processed_before_rollback = self.get_stats_for_partitioned_indexes()

        # Kill memcached on Node A so that Node B becomes master
        self.log.info("Kill Memcached process on NodeA")
        shell = RemoteMachineShellConnection(self.master)
        shell.kill_memcached()

        # Start persistence on Node B
        self.log.info("Starting persistence on NodeB")
        mem_client = MemcachedClientHelper.direct_client(
            self.input.servers[1], "default")
        mem_client.start_persistence()

        # Failover Node B
        self.log.info("Failing over NodeB")
        self.sleep(10)
        failover_task = self.cluster.async_failover(
            self.servers[:self.nodes_init], [self.servers[1]], self.graceful,
            wait_for_pending=120)

        failover_task.result()

        # Wait for a couple of mins to allow rollback to complete
        self.sleep(120)

        # Get count after rollback
        bucket_count_after_rollback, item_count_after_rollback, num_docs_processed_after_rollback = self.get_stats_for_partitioned_indexes()

        self.assertEqual(bucket_count_after_rollback, item_count_after_rollback,
                         "Partial KV Rollback not processed by Partitioned indexes")

    def test_scan_availability(self):
        create_index_query = "CREATE INDEX idx1 ON default(name,mutated) partition by hash(BASE64(meta().id)) USING GSI"
        if self.num_index_replicas:
            create_index_query += " with {'num_replica':{{0}}};".format(
                self.num_index_replicas)
        try:
            self.n1ql_helper.run_cbq_query(query=create_index_query,
                                           server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))
            self.fail(
                "index creation failed with error : {0}".format(str(ex)))

        node_out = self.servers[self.node_out]
        failover_task = self.cluster.async_failover(
            self.servers[:self.nodes_init],
            [node_out],
            self.graceful, wait_for_pending=60)

        failover_task.result()

        self.sleep(30)

        # Run query
        scan_query = "select name,mutated from default where name > 'a' and mutated >=0;"
        try:
            self.n1ql_helper.run_cbq_query(query=scan_query,
                                           server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))
            if self.num_index_replicas == 0:
                if self.expected_err_msg in str(ex):
                    pass
                else:
                    self.fail(
                        "Scan failed with unexpected error message".format(
                            str(ex)))
            else:
                self.fail("Scan failed")

    def test_scan_availability_with_network_partitioning(self):
        create_index_query = "CREATE INDEX idx1 ON default(name,mutated) partition by hash(BASE64(meta().id)) USING GSI"
        if self.num_index_replicas:
            create_index_query += " with {'num_replica':{{0}}};".format(
                self.num_index_replicas)
        try:
            self.n1ql_helper.run_cbq_query(query=create_index_query,
                                           server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))
            self.fail(
                "index creation failed with error : {0}".format(str(ex)))

        # Induce network partitioning on one of the nodes
        node_out = self.servers[self.node_out]
        self.start_firewall_on_node(node_out)

        # Run query
        scan_query = "select name,mutated from default where name > 'a' and mutated >=0;"
        try:
            self.n1ql_helper.run_cbq_query(query=scan_query,
                                           server=self.n1ql_node)
        except Exception, ex:
            self.log.info(
                "Scan failed as one indexer node was experiencing network partititioning. Error : %s",
                str(ex))

        # Heal Network Partitioning
        self.stop_firewall_on_node(node_out)

        # Re-run query
        scan_query = "select name,mutated from default where name > 'a' and mutated >=0;"
        try:
            self.n1ql_helper.run_cbq_query(query=scan_query,
                                           server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))
            if self.num_index_replicas:
                if self.expected_err_msg in str(ex):
                    pass
                else:
                    self.fail(
                        "Scan failed with unexpected error message".format(
                            str(ex)))
            else:
                self.fail("Scan failed")

    def test_index_scans(self):
        self._load_emp_dataset(end=self.num_items)

        # Create Partitioned and non-partitioned indexes

        if self.num_index_partitions > 0:
            self.rest.set_index_settings(
                {"indexer.numPartitions": self.num_index_partitions})

        create_partitioned_index1_query = "CREATE INDEX partitioned_idx1 ON default(name,dept,salary) partition by hash(name,dept,salary) USING GSI;"
        create_index1_query = "CREATE INDEX non_partitioned_idx1 ON default(name,dept,salary) USING GSI;"
        create_partitioned_index2_query = "create index partitioned_idx2 on default(name,manages.team_size) partition by hash(manages.team_size) USING GSI;"
        create_index2_query = "create index non_partitioned_idx2 on default(name,manages.team_size) USING GSI;"
        create_partitioned_index3_query = "create index partitioned_idx3 on default(name,manages.team_size) partition by hash(name,manages.team_size) USING GSI;"

        try:
            self.n1ql_helper.run_cbq_query(
                query=create_partitioned_index1_query,
                server=self.n1ql_node)
            self.n1ql_helper.run_cbq_query(query=create_index1_query,
                                           server=self.n1ql_node)
            self.n1ql_helper.run_cbq_query(
                query=create_partitioned_index2_query,
                server=self.n1ql_node)
            self.n1ql_helper.run_cbq_query(query=create_index2_query,
                                           server=self.n1ql_node)
            self.n1ql_helper.run_cbq_query(
                query=create_partitioned_index3_query,
                server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))
            self.fail(
                "index creation failed with error : {0}".format(str(ex)))

        # Scans

        queries = []

        # 1. Small lookup query with equality predicate on the partition key
        query_details = {}
        query_details[
            "query"] = "select name,dept,salary from default USE INDEX (indexname USING GSI) where name='Safiya Palmer'"
        query_details["partitioned_idx_name"] = "partitioned_idx1"
        query_details["non_partitioned_idx_name"] = "non_partitioned_idx1"
        queries.append(query_details)

        # 2. Pagination query with equality predicate on the partition key
        query_details = {}
        query_details[
            "query"] = "select name,dept,salary from default USE INDEX (indexname USING GSI) where name is not missing AND dept='HR' offset 0 limit 10"
        query_details["partitioned_idx_name"] = "partitioned_idx1"
        query_details["non_partitioned_idx_name"] = "non_partitioned_idx1"
        queries.append(query_details)

        # 3. Large aggregated query
        query_details = {}
        query_details[
            "query"] = "select count(name), dept from default USE INDEX (indexname USING GSI) where name is not missing group by dept"
        query_details["partitioned_idx_name"] = "partitioned_idx1"
        query_details["non_partitioned_idx_name"] = "non_partitioned_idx1"
        queries.append(query_details)

        # 4. Scan with large result sets
        query_details = {}
        query_details[
            "query"] = "select name,dept,salary from default USE INDEX (indexname USING GSI) where name is not missing AND salary > 10000"
        query_details["partitioned_idx_name"] = "partitioned_idx1"
        query_details["non_partitioned_idx_name"] = "non_partitioned_idx1"
        queries.append(query_details)

        # 5. Scan that does not require sorted data
        query_details = {}
        query_details[
            "query"] = "select name,dept,salary from default USE INDEX (indexname USING GSI) where name is not missing AND salary > 100000"
        query_details["partitioned_idx_name"] = "partitioned_idx1"
        query_details["non_partitioned_idx_name"] = "non_partitioned_idx1"
        queries.append(query_details)

        # 6. Scan that requires sorted data
        query_details = {}
        query_details[
            "query"] = "select name,dept,salary from default USE INDEX (indexname USING GSI) where name is not missing AND salary > 10000 order by dept asc,salary desc"
        query_details["partitioned_idx_name"] = "partitioned_idx1"
        query_details["non_partitioned_idx_name"] = "non_partitioned_idx1"
        queries.append(query_details)

        # 7. Scan with predicate on a dataset that has some values for the partition key missing, and present for some
        query_details = {}
        query_details[
            "query"] = "select name from default USE INDEX (indexname USING GSI) where name is not missing AND manages.team_size > 3"
        query_details["partitioned_idx_name"] = "partitioned_idx2"
        query_details["non_partitioned_idx_name"] = "non_partitioned_idx2"
        queries.append(query_details)

        # 8. Index partitioned on multiple keys. Scan with predicate on multiple keys with a dataset that has some values for the partition keys missing, and present for some
        query_details = {}
        query_details[
            "query"] = "select name from default USE INDEX (indexname USING GSI) where manages.team_size >= 3 and manages.team_size <= 7 and name like 'A%'"
        query_details["partitioned_idx_name"] = "partitioned_idx3"
        query_details["non_partitioned_idx_name"] = "non_partitioned_idx2"
        queries.append(query_details)

        # 9. Overlap scans on partition keys
        query_details = {}
        query_details[
            "query"] = "select name from default USE INDEX (indexname USING GSI) where name is not missing AND (manages.team_size >= 3 or manages.team_size >= 7)"
        query_details["partitioned_idx_name"] = "partitioned_idx2"
        query_details["non_partitioned_idx_name"] = "non_partitioned_idx2"
        queries.append(query_details)

        total_scans = 0
        failures = 0
        for query_details in queries:
            total_scans += 1

            try:
                query_partitioned_index = query_details["query"].replace(
                    "indexname", query_details["partitioned_idx_name"])
                query_non_partitioned_index = query_details["query"].replace(
                    "indexname", query_details["non_partitioned_idx_name"])

                result_partitioned_index = \
                    self.n1ql_helper.run_cbq_query(
                        query=query_partitioned_index,
                        min_output_size=10000000,
                        server=self.n1ql_node)["results"]
                result_non_partitioned_index = self.n1ql_helper.run_cbq_query(
                    query=query_non_partitioned_index, min_output_size=10000000,
                    server=self.n1ql_node)["results"]

                self.log.info("Partitioned : {0}".format(
                    str(result_partitioned_index.sort())))
                self.log.info("Non Partitioned : {0}".format(
                    str(result_non_partitioned_index.sort())))

                if result_partitioned_index.sort() != result_non_partitioned_index.sort():
                    failures += 1
                    self.log.info(
                        "*** This query does not return same results for partitioned and non-partitioned indexes.")
            except Exception, ex:
                self.log.info(str(ex))

        self.log.info(
            "Total scans : {0}, Matching results : {1}, Non-matching results : {2}".format(
                total_scans, total_scans - failures, failures))
        self.assertEqual(failures, 0,
                         "Some scans did not yield the same results for partitioned index and non-partitioned indexes. Details above.")

    def test_rebalance_out_with_partitioned_indexes_with_concurrent_querying(
            self):
        self._load_emp_dataset(end=self.num_items)

        # Create partitioned index
        if self.num_index_replicas > 0:
            create_index_statement = "CREATE INDEX idx1 on default(name,dept,salary) partition by hash(name) with {{'num_replica':{0}, 'num_partition':{1}}}".format(
                self.num_index_replicas, self.num_index_partitions)
            create_primary_index_statement = "CREATE PRIMARY INDEX pidx1 on default partition by hash(meta().id) with {{'num_replica':{0}, 'num_partition':{1}}}".format(
                self.num_index_replicas, self.num_index_partitions)
        else:
            create_index_statement = "CREATE INDEX idx1 on default(name,dept,salary) partition by hash(name) with {{'num_partition':{1}}}".format(
                self.num_index_replicas, self.num_index_partitions)
            create_primary_index_statement = "CREATE PRIMARY INDEX pidx1 on default partition by hash(meta().id) with {{'num_partition':{1}}}".format(
                self.num_index_replicas, self.num_index_partitions)

        try:
            self.n1ql_helper.run_cbq_query(
                query=create_index_statement,
                server=self.n1ql_node)
            self.n1ql_helper.run_cbq_query(
                query=create_primary_index_statement,
                server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))

        self.sleep(30)

        node_out = self.servers[self.node_out]
        node_out_str = node_out.ip + ":" + str(node_out.port)

        # Get Index Names
        index_names = ["idx1", "pidx1"]
        if self.num_index_replicas > 0:
            for i in range(1, self.num_index_replicas + 1):
                index_names.append("idx1 (replica {0})".format(str(i)))
                index_names.append("pidx1 (replica {0})".format(str(i)))

        self.log.info(index_names)

        # Get Stats and index partition map before rebalance
        index_data_before = {}
        for index in index_names:
            _, total_item_count_before, _ = self.get_stats_for_partitioned_indexes(
                index_name=index)
            index_data_before[index] = {}
            index_data_before[index]["item_count"] = total_item_count_before
            index_data_before[index][
                "index_metadata"] = self.rest.get_indexer_metadata()

        # start querying
        query = "select name,dept,salary from default where name is not missing and dept='HR' and salary > 120000;"
        t1 = Thread(target=self._run_queries, args=(query, 30,))
        t1.start()
        # rebalance out a indexer node when querying is in progress
        rebalance = self.cluster.async_rebalance(self.servers[:self.nodes_init],
                                                 [], [node_out])
        reached = RestHelper(self.rest).rebalance_reached()
        self.assertTrue(reached, "rebalance failed, stuck or did not complete")
        rebalance.result()
        t1.join()

        self.sleep(30)

        # Get Stats and index partition map after rebalance
        node_list = copy.deepcopy(self.node_list)
        node_list.remove(node_out_str)
        self.log.info(node_list)

        index_data_after = {}
        for index in index_names:
            _, total_item_count_after, _ = self.get_stats_for_partitioned_indexes(
                index_name=index, node_list=node_list)
            index_data_after[index] = {}
            index_data_after[index]["item_count"] = total_item_count_after
            index_data_after[index][
                "index_metadata"] = self.rest.get_indexer_metadata()

        for index in index_names:
            # Validate index item count before and after
            self.assertEqual(index_data_before[index]["item_count"],
                             index_data_after[index]["item_count"],
                             "Item count in index do not match after cluster ops.")

            # Validate host list, partition count and partition distribution
            self.assertTrue(
                self.validate_partition_distribution_after_cluster_ops(
                    index, index_data_before[index]["index_metadata"],
                    index_data_after[index]["index_metadata"], [],
                    [node_out]),
                "Partition distribution post cluster ops has some issues")

    def test_rebalance_in_with_partitioned_indexes_with_concurrent_querying(
            self):
        self._load_emp_dataset(end=self.num_items)

        # Create partitioned index
        if self.num_index_replicas > 0:
            create_index_statement = "CREATE INDEX idx1 on default(name,dept,salary) partition by hash(name) with {{'num_replica':{0}, 'num_partition':{1}}}".format(
                self.num_index_replicas, self.num_index_partitions)
            create_primary_index_statement = "CREATE PRIMARY INDEX pidx1 on default partition by hash(meta().id) with {{'num_replica':{0}, 'num_partition':{1}}}".format(
                self.num_index_replicas, self.num_index_partitions)
        else:
            create_index_statement = "CREATE INDEX idx1 on default(name,dept,salary) partition by hash(name) with {{'num_partition':{1}}}".format(
                self.num_index_replicas, self.num_index_partitions)
            create_primary_index_statement = "CREATE PRIMARY INDEX pidx1 on default partition by hash(meta().id) with {{'num_partition':{1}}}".format(
                self.num_index_replicas, self.num_index_partitions)

        try:
            self.n1ql_helper.run_cbq_query(
                query=create_index_statement,
                server=self.n1ql_node)
            self.n1ql_helper.run_cbq_query(
                query=create_primary_index_statement,
                server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))

        self.sleep(30)

        node_in = self.servers[self.nodes_init]
        node_in_str = node_in.ip + ":" + str(node_in.port)
        services_in = ["index"]

        # Get Index Names
        index_names = ["idx1", "pidx1"]
        if self.num_index_replicas > 0:
            for i in range(1, self.num_index_replicas + 1):
                index_names.append("idx1 (replica {0})".format(str(i)))
                index_names.append("pidx1 (replica {0})".format(str(i)))

        self.log.info(index_names)

        # Get Stats and index partition map before rebalance
        index_data_before = {}
        for index in index_names:
            _, total_item_count_before, _ = self.get_stats_for_partitioned_indexes(
                index_name=index)
            index_data_before[index] = {}
            index_data_before[index]["item_count"] = total_item_count_before
            index_data_before[index][
                "index_metadata"] = self.rest.get_indexer_metadata()

        # start querying
        query = "select name,dept,salary from default where name is not missing and dept='HR' and salary > 120000;"
        t1 = Thread(target=self._run_queries, args=(query, 30,))
        t1.start()
        # rebalance out a indexer node when querying is in progress
        rebalance = self.cluster.async_rebalance(self.servers[:self.nodes_init],
                                                 [node_in], [], services=services_in)
        reached = RestHelper(self.rest).rebalance_reached()
        self.assertTrue(reached, "rebalance failed, stuck or did not complete")
        rebalance.result()
        t1.join()

        self.sleep(30)

        # Get Stats and index partition map after rebalance
        node_list = copy.deepcopy(self.node_list)
        node_list.append(node_in_str)
        self.log.info(node_list)

        index_data_after = {}
        for index in index_names:
            _, total_item_count_after, _ = self.get_stats_for_partitioned_indexes(
                index_name=index, node_list=node_list)
            index_data_after[index] = {}
            index_data_after[index]["item_count"] = total_item_count_after
            index_data_after[index][
                "index_metadata"] = self.rest.get_indexer_metadata()

        for index in index_names:
            # Validate index item count before and after
            self.assertEqual(index_data_before[index]["item_count"],
                             index_data_after[index]["item_count"],
                             "Item count in index do not match after cluster ops.")

            # Validate host list, partition count and partition distribution
            self.assertTrue(
                self.validate_partition_distribution_after_cluster_ops(
                    index, index_data_before[index]["index_metadata"],
                    index_data_after[index]["index_metadata"], [node_in],
                    []),
                "Partition distribution post cluster ops has some issues")

    def test_swap_rebalance_with_partitioned_indexes_with_concurrent_querying(
            self):
        self._load_emp_dataset(end=self.num_items)

        # Create partitioned index
        if self.num_index_replicas > 0:
            create_index_statement = "CREATE INDEX idx1 on default(name,dept,salary) partition by hash(name) with {{'num_replica':{0}, 'num_partition':{1}}}".format(
                self.num_index_replicas, self.num_index_partitions)
            create_primary_index_statement = "CREATE PRIMARY INDEX pidx1 on default partition by hash(meta().id) with {{'num_replica':{0}, 'num_partition':{1}}}".format(
                self.num_index_replicas, self.num_index_partitions)
        else:
            create_index_statement = "CREATE INDEX idx1 on default(name,dept,salary) partition by hash(name) with {{'num_partition':{1}}}".format(
                self.num_index_replicas, self.num_index_partitions)
            create_primary_index_statement = "CREATE PRIMARY INDEX pidx1 on default partition by hash(meta().id) with {{'num_partition':{1}}}".format(
                self.num_index_replicas, self.num_index_partitions)

        try:
            self.n1ql_helper.run_cbq_query(
                query=create_index_statement,
                server=self.n1ql_node)
            self.n1ql_helper.run_cbq_query(
                query=create_primary_index_statement,
                server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))

        self.sleep(30)

        node_out = self.servers[self.node_out]
        node_out_str = node_out.ip + ":" + str(node_out.port)

        node_in = self.servers[self.nodes_init]
        node_in_str = node_in.ip + ":" + str(node_in.port)
        services_in = ["index"]

        # Get Index Names
        index_names = ["idx1", "pidx1"]
        if self.num_index_replicas > 0:
            for i in range(1, self.num_index_replicas + 1):
                index_names.append("idx1 (replica {0})".format(str(i)))
                index_names.append("pidx1 (replica {0})".format(str(i)))

        self.log.info(index_names)

        # Get Stats and index partition map before rebalance
        index_data_before = {}
        for index in index_names:
            _, total_item_count_before, _ = self.get_stats_for_partitioned_indexes(
                index_name=index)
            index_data_before[index] = {}
            index_data_before[index]["item_count"] = total_item_count_before
            index_data_before[index][
                "index_metadata"] = self.rest.get_indexer_metadata()

        # start querying
        query = "select name,dept,salary from default where name is not missing and dept='HR' and salary > 120000;"
        t1 = Thread(target=self._run_queries, args=(query, 30,))
        t1.start()
        # rebalance out a indexer node when querying is in progress
        rebalance = self.cluster.async_rebalance(self.servers[:self.nodes_init],
                                                 [node_in], [node_out], services=services_in)
        reached = RestHelper(self.rest).rebalance_reached()
        self.assertTrue(reached, "rebalance failed, stuck or did not complete")
        rebalance.result()
        t1.join()

        self.sleep(30)

        # Get Stats and index partition map after rebalance
        node_list = copy.deepcopy(self.node_list)
        node_list.append(node_in_str)
        node_list.remove(node_out_str)

        index_data_after = {}
        for index in index_names:
            _, total_item_count_after, _ = self.get_stats_for_partitioned_indexes(
                index_name=index, node_list=node_list)
            index_data_after[index] = {}
            index_data_after[index]["item_count"] = total_item_count_after
            index_data_after[index][
                "index_metadata"] = self.rest.get_indexer_metadata()

        for index in index_names:
            # Validate index item count before and after
            self.assertEqual(index_data_before[index]["item_count"],
                             index_data_after[index]["item_count"],
                             "Item count in index do not match after cluster ops.")

            # Validate host list, partition count and partition distribution
            self.assertTrue(
                self.validate_partition_distribution_after_cluster_ops(
                    index, index_data_before[index]["index_metadata"],
                    index_data_after[index]["index_metadata"], [node_in],
                    [node_out]),
                "Partition distribution post cluster ops has some issues")

    def test_failover_with_partitioned_indexes_with_concurrent_querying(
            self):
        self._load_emp_dataset(end=self.num_items)

        # Create partitioned index
        if self.num_index_replicas > 0:
            create_index_statement = "CREATE INDEX idx1 on default(name,dept,salary) partition by hash(name) with {{'num_replica':{0}, 'num_partition':{1}}}".format(
                self.num_index_replicas, self.num_index_partitions)
            create_primary_index_statement = "CREATE PRIMARY INDEX pidx1 on default partition by hash(meta().id) with {{'num_replica':{0}, 'num_partition':{1}}}".format(
                self.num_index_replicas, self.num_index_partitions)
        else:
            create_index_statement = "CREATE INDEX idx1 on default(name,dept,salary) partition by hash(name) with {{'num_partition':{1}}}".format(
                self.num_index_replicas, self.num_index_partitions)
            create_primary_index_statement = "CREATE PRIMARY INDEX pidx1 on default partition by hash(meta().id) with {{'num_partition':{1}}}".format(
                self.num_index_replicas, self.num_index_partitions)

        try:
            self.n1ql_helper.run_cbq_query(
                query=create_index_statement,
                server=self.n1ql_node)
            self.n1ql_helper.run_cbq_query(
                query=create_primary_index_statement,
                server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))

        self.sleep(30)

        node_out = self.servers[self.node_out]
        node_out_str = node_out.ip + ":" + str(node_out.port)

        # Get Index Names
        index_names = ["idx1", "pidx1"]
        if self.num_index_replicas > 0:
            for i in range(1, self.num_index_replicas + 1):
                index_names.append("idx1 (replica {0})".format(str(i)))
                index_names.append("pidx1 (replica {0})".format(str(i)))

        self.log.info(index_names)

        # Get Stats and index partition map before rebalance
        index_data_before = {}
        for index in index_names:
            _, total_item_count_before, _ = self.get_stats_for_partitioned_indexes(
                index_name=index)
            index_data_before[index] = {}
            index_data_before[index]["item_count"] = total_item_count_before
            index_data_before[index][
                "index_metadata"] = self.rest.get_indexer_metadata()

        # start querying
        query = "select name,dept,salary from default where name is not missing and dept='HR' and salary > 120000;"
        t1 = Thread(target=self._run_queries, args=(query, 30,))
        t1.start()

        # failover and rebalance out a indexer node when querying is in progress
        failover_task = self.cluster.async_failover(
            self.servers[:self.nodes_init],
            [node_out],
            self.graceful, wait_for_pending=180)

        failover_task.result()

        rebalance = self.cluster.async_rebalance(self.servers[:self.nodes_init],
                                                 [], [node_out])
        reached = RestHelper(self.rest).rebalance_reached()
        self.assertTrue(reached, "rebalance failed, stuck or did not complete")
        rebalance.result()
        t1.join()

        self.sleep(30)

        # Get Stats and index partition map after rebalance
        node_list = copy.deepcopy(self.node_list)
        node_list.remove(node_out_str)
        self.log.info(node_list)

        index_data_after = {}
        for index in index_names:
            _, total_item_count_after, _ = self.get_stats_for_partitioned_indexes(
                index_name=index, node_list=node_list)
            index_data_after[index] = {}
            index_data_after[index]["item_count"] = total_item_count_after
            index_data_after[index][
                "index_metadata"] = self.rest.get_indexer_metadata()

        for index in index_names:
            # Validate index item count before and after
            self.assertEqual(index_data_before[index]["item_count"],
                             index_data_after[index]["item_count"],
                             "Item count in index do not match after cluster ops.")

            # Validate host list, partition count and partition distribution
            self.assertTrue(
                self.validate_partition_distribution_after_cluster_ops(
                    index, index_data_before[index]["index_metadata"],
                    index_data_after[index]["index_metadata"], [],
                    [node_out]),
                "Partition distribution post cluster ops has some issues")

    def test_failover_addback_with_partitioned_indexes_with_concurrent_querying(
            self):
        self._load_emp_dataset(end=self.num_items)

        # Create partitioned index
        if self.num_index_replicas > 0:
            create_index_statement = "CREATE INDEX idx1 on default(name,dept,salary) partition by hash(name) with {{'num_replica':{0}, 'num_partition':{1}}}".format(
                self.num_index_replicas, self.num_index_partitions)
            create_primary_index_statement = "CREATE PRIMARY INDEX pidx1 on default partition by hash(meta().id) with {{'num_replica':{0}, 'num_partition':{1}}}".format(
                self.num_index_replicas, self.num_index_partitions)
        else:
            create_index_statement = "CREATE INDEX idx1 on default(name,dept,salary) partition by hash(name) with {{'num_partition':{1}}}".format(
                self.num_index_replicas, self.num_index_partitions)
            create_primary_index_statement = "CREATE PRIMARY INDEX pidx1 on default partition by hash(meta().id) with {{'num_partition':{1}}}".format(
                self.num_index_replicas, self.num_index_partitions)

        try:
            self.n1ql_helper.run_cbq_query(
                query=create_index_statement,
                server=self.n1ql_node)
            self.n1ql_helper.run_cbq_query(
                query=create_primary_index_statement,
                server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))

        self.sleep(30)

        node_out = self.servers[self.node_out]
        node_out_str = node_out.ip + ":" + str(node_out.port)

        # Get Index Names
        index_names = ["idx1", "pidx1"]
        if self.num_index_replicas > 0:
            for i in range(1, self.num_index_replicas + 1):
                index_names.append("idx1 (replica {0})".format(str(i)))
                index_names.append("pidx1 (replica {0})".format(str(i)))

        self.log.info(index_names)

        # Get Stats and index partition map before rebalance
        index_data_before = {}
        for index in index_names:
            _, total_item_count_before, _ = self.get_stats_for_partitioned_indexes(
                index_name=index)
            index_data_before[index] = {}
            index_data_before[index]["item_count"] = total_item_count_before
            index_data_before[index][
                "index_metadata"] = self.rest.get_indexer_metadata()

        # start querying
        query = "select name,dept,salary from default where name is not missing and dept='HR' and salary > 120000;"
        t1 = Thread(target=self._run_queries, args=(query, 30,))
        t1.start()

        # failover and rebalance out a indexer node when querying is in progress
        nodes_all = self.rest.node_statuses()
        for node in nodes_all:
            if node.ip == node_out.ip:
                break

        failover_task = self.cluster.async_failover(
            self.servers[:self.nodes_init],
            [node_out],
            self.graceful, wait_for_pending=180)

        failover_task.result()

        self.rest.set_recovery_type(node.id, self.recovery_type)
        self.rest.add_back_node(node.id)

        rebalance = self.cluster.async_rebalance(self.servers[:self.nodes_init],
                                                 [], [])

        reached = RestHelper(self.rest).rebalance_reached()
        self.assertTrue(reached, "rebalance failed, stuck or did not complete")
        rebalance.result()
        t1.join()

        self.sleep(30)

        # Get Stats and index partition map after rebalance
        index_data_after = {}
        for index in index_names:
            _, total_item_count_after, _ = self.get_stats_for_partitioned_indexes(
                index_name=index)
            index_data_after[index] = {}
            index_data_after[index]["item_count"] = total_item_count_after
            index_data_after[index][
                "index_metadata"] = self.rest.get_indexer_metadata()

        for index in index_names:
            # Validate index item count before and after
            self.assertEqual(index_data_before[index]["item_count"],
                             index_data_after[index]["item_count"],
                             "Item count in index do not match after cluster ops.")

            # Validate host list, partition count and partition distribution
            self.assertTrue(
                self.validate_partition_distribution_after_cluster_ops(
                    index, index_data_before[index]["index_metadata"],
                    index_data_after[index]["index_metadata"], [],
                    []),
                "Partition distribution post cluster ops has some issues")

    def test_kv_rebalance_out_with_partitioned_indexes_with_concurrent_querying(
            self):
        self._load_emp_dataset(end=self.num_items)

        # Create partitioned index
        if self.num_index_replicas > 0:
            create_index_statement = "CREATE INDEX idx1 on default(name,dept,salary) partition by hash(name) with {{'num_replica':{0}, 'num_partition':{1}}}".format(
                self.num_index_replicas, self.num_index_partitions)
            create_primary_index_statement = "CREATE PRIMARY INDEX pidx1 on default partition by hash(meta().id) with {{'num_replica':{0}, 'num_partition':{1}}}".format(
                self.num_index_replicas, self.num_index_partitions)
        else:
            create_index_statement = "CREATE INDEX idx1 on default(name,dept,salary) partition by hash(name) with {{'num_partition':{1}}}".format(
                self.num_index_replicas, self.num_index_partitions)
            create_primary_index_statement = "CREATE PRIMARY INDEX pidx1 on default partition by hash(meta().id) with {{'num_partition':{1}}}".format(
                self.num_index_replicas, self.num_index_partitions)

        try:
            self.n1ql_helper.run_cbq_query(
                query=create_index_statement,
                server=self.n1ql_node)
            self.n1ql_helper.run_cbq_query(
                query=create_primary_index_statement,
                server=self.n1ql_node)
        except Exception, ex:
            self.log.info(str(ex))

        self.sleep(30)

        node_out = self.servers[self.node_out]
        node_out_str = node_out.ip + ":" + str(node_out.port)

        # Get Index Names
        index_names = ["idx1", "pidx1"]
        if self.num_index_replicas > 0:
            for i in range(1, self.num_index_replicas + 1):
                index_names.append("idx1 (replica {0})".format(str(i)))
                index_names.append("pidx1 (replica {0})".format(str(i)))

        self.log.info(index_names)

        # Get Stats and index partition map before rebalance
        index_data_before = {}
        for index in index_names:
            _, total_item_count_before, _ = self.get_stats_for_partitioned_indexes(
                index_name=index)
            index_data_before[index] = {}
            index_data_before[index]["item_count"] = total_item_count_before
            index_data_before[index][
                "index_metadata"] = self.rest.get_indexer_metadata()

        # start querying
        query = "select name,dept,salary from default where name is not missing and dept='HR' and salary > 120000;"
        t1 = Thread(target=self._run_queries, args=(query, 30,))
        t1.start()
        # rebalance out a indexer node when querying is in progress
        rebalance = self.cluster.async_rebalance(self.servers[:self.nodes_init],
                                                 [], [node_out])
        reached = RestHelper(self.rest).rebalance_reached()
        self.assertTrue(reached, "rebalance failed, stuck or did not complete")
        rebalance.result()
        t1.join()

        self.sleep(30)

        # Get Stats and index partition map after rebalance

        index_data_after = {}
        for index in index_names:
            _, total_item_count_after, _ = self.get_stats_for_partitioned_indexes(
                index_name=index)
            index_data_after[index] = {}
            index_data_after[index]["item_count"] = total_item_count_after
            index_data_after[index][
                "index_metadata"] = self.rest.get_indexer_metadata()

        for index in index_names:
            # Validate index item count before and after
            self.assertEqual(index_data_before[index]["item_count"],
                             index_data_after[index]["item_count"],
                             "Item count in index do not match after cluster ops.")

            # Validate host list, partition count and partition distribution
            self.assertTrue(
                self.validate_partition_distribution_after_cluster_ops(
                    index, index_data_before[index]["index_metadata"],
                    index_data_after[index]["index_metadata"], [],
                    []),
                "Partition distribution post cluster ops has some issues")

    def get_stats_for_partitioned_indexes(self, bucket_name="default",
                                          index_name=None, node_list=None):
        if node_list == None:
            node_list = self.node_list

        bucket_item_count = self.get_item_count(self.servers[0], bucket_name)

        index_stats = self.get_index_stats(perNode=True)
        total_item_count = 0
        total_items_processed = 0
        for node in node_list:
            if not index_name:
                index_names = []
                for key in index_stats[node][bucket_name]:
                    index_names.append(key)
                index_name = index_names[0]
            try:
                total_item_count += index_stats[node][bucket_name][index_name][
                    "items_count"]
                total_items_processed = \
                    index_stats[node][bucket_name][index_name][
                        "num_docs_processed"]
            except Exception, ex:
                self.log.info(str(ex))

        self.log.info(
            "Index {0} : Total Item Count={1} Total Items Processed={2}".format(
                index_name, str(total_item_count), str(total_items_processed)))

        return (bucket_item_count, total_item_count, total_items_processed)

    # Description : Validate index metadata : num_partitions, index status, index existence
    def validate_partitioned_indexes(self, index_details, index_map,
                                     index_metadata):

        isIndexPresent = False
        isNumPartitionsCorrect = False
        isDeferBuildCorrect = False

        # Check if index exists
        for index in index_metadata["status"]:
            if index["name"] == index_details["index_name"]:
                isIndexPresent = True
                # If num-partitions are set, check no. of partitions
                expected_num_partitions = 16
                if index_details["num_partitions"] > 0:
                    expected_num_partitions = index_details["num_partitions"]

                if index["partitioned"] and index[
                    "numPartition"] == expected_num_partitions:
                    isNumPartitionsCorrect = True
                else:
                    self.log.info(
                        "Index {0} on /getIndexStatus : Partitioned={1}, num_partition={2}.. Expected numPartitions={3}".format(
                            index["name"], index["partitioned"],
                            index["numPartition"],
                            index_details["num_partitions"]))

                if index_details["defer_build"] == True and index[
                    "status"] == "Created":
                    isDeferBuildCorrect = True
                elif index_details["defer_build"] == False and index[
                    "status"] == "Ready":
                    isDeferBuildCorrect = True
                else:
                    self.log.info(
                        "Incorrect build status for index created with defer_build=True. Status for {0} is {1}".format(
                            index["name"], index["status"]))

        if not isIndexPresent:
            self.log.info("Index not listed in /getIndexStatus")

        return isIndexPresent and isNumPartitionsCorrect and isDeferBuildCorrect

    # Description : Checks if same host contains same partitions from different replica, and also if for each replica, if the partitions are distributed across nodes
    def validate_partition_map(self, index_metadata, index_name, num_replica,
                               num_partitions):
        index_names = []
        index_names.append(index_name)

        hosts = index_metadata["status"][0]["hosts"]

        for i in range(1, num_replica + 1):
            index_names.append(index_name + " (replica {0})".format(str(i)))

        partition_validation_per_host = True
        for host in hosts:
            pmap_host = []
            for idx_name in index_names:
                for index in index_metadata["status"]:
                    if index["name"] == idx_name:
                        pmap_host += index["partitionMap"][host]

            self.log.info(
                "List of partitions on {0} : {1}".format(host, pmap_host))
            if len(set(pmap_host)) != num_partitions:
                partition_validation_per_host &= False
                self.log.info(
                    "Partitions on {0} for all replicas are not correct".format(
                        host))

        partitions_distributed_for_index = True
        for idx_name in index_names:
            for index in index_metadata["status"]:
                if index["name"] == idx_name:
                    totalPartitions = 0
                    for host in hosts:
                        if not index["partitionMap"][host]:
                            partitions_distributed_for_index &= False
                        totalPartitions += len(index["partitionMap"][host])

                    partitions_distributed_for_index &= (
                        totalPartitions == num_partitions)

        return partition_validation_per_host & partitions_distributed_for_index

    def validate_partition_distribution_after_cluster_ops(self, index_name,
                                                          map_before_rebalance,
                                                          map_after_rebalance,
                                                          nodes_in, nodes_out):

        # Check for number of partitions before and after rebalance
        # Check the host list before rebalance and after rebalance, and see if the incoming or outgoing node is added/removed from the host list
        # Check for partition distribution across all indexer nodes

        for index in map_before_rebalance["status"]:
            if index["name"] == index_name:
                host_list_before = index["hosts"]
                num_partitions_before = index["numPartition"]
                partition_map_before = index["partitionMap"]

        for index in map_after_rebalance["status"]:
            if index["name"] == index_name:
                host_list_after = index["hosts"]
                num_partitions_after = index["numPartition"]
                partition_map_after = index["partitionMap"]

        is_num_partitions_equal = False
        if num_partitions_before == num_partitions_after:
            is_num_partitions_equal = True
        else:
            self.log.info(
                "Number of partitions before and after cluster operations is not equal. Some partitions missing/extra.")
            self.log.info(
                "Num Partitions Before : {0}, Num Partitions After : {1}".format(
                    num_partitions_before, num_partitions_after))

        expected_host_list_after = copy.deepcopy(host_list_before)
        for node in nodes_in:
            node_str = node.ip + ":" + str(node.port)
            expected_host_list_after.append(node_str)

        for node in nodes_out:
            node_str = node.ip + ":" + str(node.port)
            expected_host_list_after.remove(node_str)

        is_node_list_correct = False
        if (expected_host_list_after.sort() == host_list_after.sort()):
            is_node_list_correct = True
        else:
            self.log.info(
                "Host list for index is not expected after cluster operations.")
            self.log.info("Expected Nodes : {0}, Actual nodes : {1}",
                          format(str(expected_host_list_after),
                                 str(host_list_after)))

        is_partitions_distributed = False
        pmap_host_list = partition_map_after.keys()
        if pmap_host_list.sort() == host_list_after.sort():
            is_partitions_distributed = True
        else:
            self.log.info(
                "Partitions not distributed correctly post cluster ops")

        return is_num_partitions_equal & is_node_list_correct & is_partitions_distributed

    # Description : Returns a list of create index statements generated randomly for emp dataset.
    #               The create index statements are generated by randomizing various parts of the statements like list of
    #               index keys, partition keys, primary/secondary indexes, deferred index, partial index, replica index, etc.
    def generate_random_create_index_statements(self, bucketname="default",
                                                idx_node_list=None,
                                                num_statements=1):
        num_idx_nodes = len(idx_node_list)

        emp_fields = {
            'text': ["name", "dept", "languages_known", "email", "meta().id"],
            'number': ["mutated", "salary"],
            'boolean': ["is_manager"],
            'datetime': ["join_date"],
            'object': ["manages"]  # denote nested fields
        }

        emp_nested_fields = {
            'manages': {
                'text': ["reports"],
                'number': ["team_size"]
            }
        }

        index_variations_list = ["num_partitions", "num_replica", "defer_build",
                                 "partial_index", "primary_index", "nodes",
                                 "sizing_estimates"]

        all_emp_fields = ["name", "dept", "languages_known", "email", "mutated",
                          "salary", "is_manager", "join_date", "reports",
                          "team_size"]

        partition_key_type_list = ["leading_key", "trailing_key",
                                   "function_applied_key",
                                   "document_id", "function_applied_doc_id"]

        index_details = []

        for i in range(num_statements):

            random.seed()

            # 1. Generate a random no. of fields to be indexed
            num_index_keys = random.randint(1, len(all_emp_fields) - 1)

            # 2. Generate random fields
            index_fields = []
            for index in range(0, num_index_keys):
                index_field_list_idx = random.randint(0, len(
                    all_emp_fields) - 1)
                if all_emp_fields[
                    index_field_list_idx] not in index_fields:
                    index_fields.append(
                        all_emp_fields[index_field_list_idx])
                else:
                    # Generate a random index again
                    index_field_list_idx = random.randint(0,
                                                          len(
                                                              all_emp_fields) - 1)
                    if all_emp_fields[
                        index_field_list_idx] not in index_fields:
                        index_fields.append(
                            all_emp_fields[index_field_list_idx])

            # 3. Generate a random no. for no. of partition keys (this should be < #1)
            if num_index_keys > 1:
                num_partition_keys = random.randint(1, num_index_keys - 1)
            else:
                num_partition_keys = num_index_keys

            # 4. For each partition key, randomly select a partition key type from the list and generate a partition key with it
            partition_keys = []
            for index in range(num_partition_keys):
                key = None
                partition_key_type = partition_key_type_list[
                    random.randint(0, len(partition_key_type_list) - 1)]

                if partition_key_type == partition_key_type_list[0]:
                    key = index_fields[0]

                if partition_key_type == partition_key_type_list[1]:
                    if len(index_fields) > 1:
                        randval = random.randint(1, len(index_fields) - 1)
                        key = index_fields[randval]

                if partition_key_type == partition_key_type_list[2]:
                    idx_key = index_fields[
                        random.randint(0, len(index_fields) - 1)]

                    if idx_key in emp_fields["text"]:
                        key = ("LOWER({0})".format(idx_key))
                    elif idx_key in emp_fields["number"]:
                        key = ("({0} % 10) + ({0} * 2) ").format(idx_key)
                    elif idx_key in emp_fields["boolean"]:
                        key = ("NOT {0}".format(idx_key))
                    elif idx_key in emp_fields["datetime"]:
                        key = ("DATE_ADD_STR({0},-1,'year')".format(idx_key))
                    elif idx_key in emp_nested_fields["manages"]["text"]:
                        key = ("LOWER({0})".format(idx_key))
                    elif idx_key in emp_nested_fields["manages"]["number"]:
                        key = ("({0} % 10) + ({0} * 2)").format(idx_key)

                if partition_key_type == partition_key_type_list[3]:
                    key = "meta().id"

                if partition_key_type == partition_key_type_list[4]:
                    key = "SUBSTR(meta().id, POSITION(meta().id, '__')+2)"

                if key is not None and key not in partition_keys:
                    partition_keys.append(key)

            # 6. Choose other variation in queries from the list.
            num_index_variations = random.randint(0, len(
                index_variations_list) - 1)
            index_variations = []
            for index in range(num_index_variations):
                index_variation = index_variations_list[
                    random.randint(0, len(index_variations_list) - 1)]
                if index_variation not in index_variations:
                    index_variations.append(index_variation)

            # Primary indexes cannot be partial, so remove partial index if primary index is in the list
            if ("primary_index" in index_variations) and (
                        "partial_index" in index_variations):
                index_variations.remove("partial_index")

            # 7. Build create index queries.
            index_name = "idx" + str(random.randint(0, 1000000))
            if "primary_index" in index_variations:
                create_index_statement = "CREATE PRIMARY INDEX {0} on {1}".format(
                    index_name, bucketname)
            else:
                create_index_statement = "CREATE INDEX {0} on {1}(".format(
                    index_name, bucketname)
                create_index_statement += ",".join(index_fields) + ")"

            create_index_statement += " partition by hash("
            create_index_statement += ",".join(partition_keys) + ")"

            if "partial_index" in index_variations:
                create_index_statement += " where meta().id > 10"

            with_list = ["num_partitions", "num_replica", "defer_build",
                         "nodes", "sizing_estimates"]

            num_partitions = 0
            num_replica = 0
            defer_build = False
            nodes = []
            if (any(x in index_variations for x in with_list)):
                with_statement = []
                create_index_statement += " with {"
                if "num_partitions" in index_variations:
                    num_partitions = random.randint(4, 100)
                    with_statement.append(
                        "'num_partition':{0}".format(num_partitions))
                if "num_replica" in index_variations:
                    # We do not want 'num_replica' and 'nodes' both in the with clause, as it can cause errors if they do not match.
                    if "nodes" in index_variations:
                        index_variations.remove("nodes")

                    num_replica = random.randint(1, num_idx_nodes - 1)
                    with_statement.append(
                        "'num_replica':{0}".format(num_replica))
                if "defer_build" in index_variations:
                    defer_build = True
                    with_statement.append("'defer_build':true")
                if "sizing_estimates" in index_variations:
                    with_statement.append("'secKeySize':20")
                    with_statement.append("'docKeySize':20")
                    with_statement.append("'arrSize':10")
                if "nodes" in index_variations:
                    num_nodes = random.randint(1, num_idx_nodes - 1)
                    for i in range(0, num_nodes):
                        node = idx_node_list[
                            random.randint(0, num_idx_nodes - 1)]
                        if node not in nodes:
                            nodes.append(node)

                    node_list_str = ""
                    if nodes is not None and len(nodes) > 1:
                        node_list_str = "\"" + "\",\"".join(nodes) + "\""
                    else:
                        node_list_str = "\"" + nodes[0] + "\""
                    with_statement.append("'nodes':[{0}]".format(node_list_str))

                create_index_statement += ",".join(with_statement) + "}"

            index_detail = {}
            index_detail["index_name"] = index_name
            index_detail["num_partitions"] = num_partitions
            index_detail["num_replica"] = num_replica
            index_detail["defer_build"] = defer_build
            index_detail["index_definition"] = create_index_statement
            index_detail["nodes"] = nodes

            if partition_keys is not None:
                index_details.append(index_detail)
            else:
                self.log.info(
                    "Generated a malformed index definition. Discarding it.")

        return index_details

    def _load_emp_dataset(self, op_type="create", expiration=0, start=0,
                          end=1000):
        # Load Emp Dataset
        self.cluster.bucket_flush(self.master)

        self._kv_gen = JsonDocGenerator("emp_",
                                        encoding="utf-8",
                                        start=start,
                                        end=end)
        gen = copy.deepcopy(self._kv_gen)

        self._load_bucket(self.buckets[0], self.servers[0], gen, op_type,
                          expiration)

    def _run_queries(self, query, count=10):
        for i in range(0, count):
            try:
                self.n1ql_helper.run_cbq_query(query=query,
                                               server=self.n1ql_node)
            except Exception, ex:
                self.log.info(str(ex))
                raise Exception("query failed")
            self.sleep(1)
