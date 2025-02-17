from mango import Agent
import asyncio
import pandapower as pp
import pandapower.networks as ppnet
import simbench as sb
import numpy as np
import logging
from messages.message_classes import (
    TimeStepMessage,
    TimeStepReply,
    AgentAddress,
    RegistrationMessage,
    RegistrationReply,
    LocalResidualScheduleMessage,
    ControlMechanismMessage,
)
from util import time_int_to_str, read_grid_config

# Pandapower Copyright (c) 2018 by University of Kassel and Fraunhofer IEE

ONE_DAY_IN_SECONDS = 24 * 60 * 60


class CentralInstance(Agent):
    # initialize grid component limits for buses and lines
    BUS_LV_VM_MIN = 0.95
    BUS_LV_VM_MAX = 1.05
    LINE_LV_LOAD_MAX = 100

    def __init__(self, container):
        # We must pass a reference of the container to "mango.Agent":
        super().__init__(container)

        # read config
        self.grid_config = read_grid_config()

        # store length of one simulation step
        self.step_size_s = 15 * 60  # 15 min steps, 900 seconds
        self.steps_day = int(ONE_DAY_IN_SECONDS / self.step_size_s)

        # initialize grid for simulation
        self.init_grid(grid_name=self.grid_config["GRID"])

        # initialize list to store result data for buses and lines
        self.result_timeseries_bus_vm_pu = {}
        self.result_timeseries_line_load = {}
        for i_bus in range(len(self.grid.bus)):
            self.result_timeseries_bus_vm_pu[self.grid.bus.loc[i_bus, "name"]] = []
        for i_line in range(len(self.grid.line)):
            self.result_timeseries_line_load[self.grid.line.loc[i_line, "name"]] = []
        for i_trafo in range(len(self.grid.trafo)):
            self.result_timeseries_line_load[self.grid.trafo.loc[i_trafo, "name"]] = []

        # initialize grid congestions results
        self.congestions = []

        # store participants and their connection to the buses and check for faults
        self.num_households = self.grid_config["NUM_HOUSEHOLDS"]
        self.num_participants = self.grid_config["NUM_PARTICIPANTS"]
        self.current_participants = 0
        if self.num_households > len(self.load_bus_names):
            raise ValueError("Creating grid with more households than load buses!")
        if self.num_participants > self.num_households:
            raise ValueError("Creating grid with more participants than households!")

        # initialize coordination of participants to load buses
        self.load_participant_coord = {}

        # initialize input for recieved schedules from participants
        self.received_schedules = {}

        # initialize status of whether calculation of this time step is finished
        self.time_step_done = False

        # store type of current control form
        self.control_type = self.grid_config["CONTROL_TYPE"]
        self.control_conf = self.grid_config["CONFIG_CONTROL"]
        self.control_signals = {}

    def handle_message(self, content, meta):
        # This method defines what the agent will do with incoming messages.
        sender_id = meta.get("sender_id", None)
        sender_addr = meta.get("sender_addr", None)
        sender = AgentAddress(sender_addr[0], sender_addr[1], sender_id)
        acl_meta = {"sender_id": self.aid, "sender_addr": self.addr}

        if isinstance(content, TimeStepMessage):
            self.schedule_instant_task(
                self.compute_time_step(content.time, sender, content.c_id)
            )

        # no need to sync this because we run it synchronized from the simulation setup file
        # if this was truly asynchronous we would have to safeguard it
        if isinstance(content, RegistrationMessage):
            if self.current_participants < self.num_participants:
                self.add_participant(sender)
                content = RegistrationReply(True)
            else:
                content = RegistrationReply(False)

            self.schedule_instant_acl_message(
                content,
                (sender.host, sender.port),
                sender.agent_id,
                acl_metadata=acl_meta,
            )

        if isinstance(content, LocalResidualScheduleMessage):
            logging.info(f"Got residual schedule message from agent: {sender}")

            if content.timestamp not in self.received_schedules.keys():
                self.received_schedules[content.timestamp] = {}

            self.received_schedules[content.timestamp][
                sender
            ] = content.residual_schedule

    def add_participant(self, participant_address):
        if self.current_participants < self.num_participants:
            self.load_participant_coord[participant_address] = self.load_bus_names[
                self.current_participants
            ]
            self.current_participants += 1
        else:
            logging.warning(
                "Trying to register too many participants. Excess participants will be ignored by the central instance."
            )

    async def get_participant_schedules(self, timestamp):
        # TODO make this nicer with futures?
        while timestamp not in self.received_schedules.keys():
            await asyncio.sleep(0.01)

        while len(self.received_schedules[timestamp].keys()) < self.num_participants:
            await asyncio.sleep(0.01)

    def check_schedule_ok(self, list_results_bus, list_results_line) -> bool:
        """Check grid results for congestion and returns status.

        Returns:
            bool: True in case of no congestion, false otherwise
        """
        # reset congestion results
        self.congestions = []
        status_no_congestion = True

        # first check whether step is generation or demand step
        mean_voltage_for_steps = np.zeros(self.steps_day)
        for step in range(len(mean_voltage_for_steps)):
            mean_voltage_for_steps[step] = np.mean(
                [vm_list[step] for vm_list in list_results_bus.values()]
            )

        # go by both lists of results and check for any limit violation
        for bus_id, bus_vm_list in list_results_bus.items():
            for step, value in enumerate(bus_vm_list):
                if value > self.BUS_LV_VM_MAX:
                    self.congestions.append(
                        {
                            "step": step,
                            "comp_id": bus_id,
                            "val": value,
                            "curtail": "gen",
                        }
                    )
                    status_no_congestion = False
                elif value < self.BUS_LV_VM_MIN:
                    self.congestions.append(
                        {
                            "step": step,
                            "comp_id": bus_id,
                            "val": value,
                            "curtail": "dem",
                        }
                    )
                    status_no_congestion = False
        for line_id, line_load_list in list_results_line.items():
            for step, value in enumerate(line_load_list):
                if value > self.LINE_LV_LOAD_MAX:
                    if (
                        mean_voltage_for_steps[step] > 1.0
                    ):  # generation-driven congestion
                        self.congestions.append(
                            {
                                "step": step,
                                "comp_id": line_id,
                                "val": value,
                                "curtail": "gen",
                            }
                        )
                    else:  # demand-driven congestion
                        self.congestions.append(
                            {
                                "step": step,
                                "comp_id": line_id,
                                "val": value,
                                "curtail": "dem",
                            }
                        )
                    status_no_congestion = False

        logging.info(
            f"Central Instance - {len(self.congestions)} congestions detected."
        )
        logging.info(f"Central Instance - congested steps: {self.congestions}")

        return status_no_congestion

    async def calculate_grid_schedule(self, timestamp):
        # NOTE:
        # Assumes all relevant participant schedules are now present in:
        # self.received_schedules[timestamp]
        #
        #
        # self.load_participant_coord[participant_address] - maps participant address to bus number
        # self.received_schedules[timestamp][participan_address] - contains the corresponding schedule
        #
        # for simplicity, parse these two things into one dict here:
        bus_to_schedule = {}
        for addr, bus_id in self.load_participant_coord.items():
            bus_to_schedule[bus_id] = self.received_schedules[timestamp][addr]

        # print(bus_to_schedule["loadbus_1_2"])

        # initialize schedule results list
        list_results_bus = {}
        list_results_line = {}
        for bus_id in self.result_timeseries_bus_vm_pu.keys():
            list_results_bus[bus_id] = []
        for line_id in self.result_timeseries_line_load.keys():
            list_results_line[line_id] = []

        for i in range(self.steps_day):
            # form: power_for_buses = {"loadbus_1_1": 2, ...}
            bus_to_power = {}
            for bus_id in bus_to_schedule.keys():
                bus_to_power[bus_id] = bus_to_schedule[bus_id][i]

            # set the inputs from the agents schedules
            self.set_inputs(data_for_buses=bus_to_power)

            # calculate the powerflow
            self.calc_grid_powerflow()

            # store the results of the powerflow
            self.store_grid_results()

            # set results into result saving lists
            for bus_id in self.grid_results_bus.keys():
                list_results_bus[bus_id].append(self.grid_results_bus[bus_id])
            for line_id in self.grid_results_line.keys():
                list_results_line[line_id].append(self.grid_results_line[line_id])

        return list_results_bus, list_results_line

    def clear_local_schedules(self, timestamp):
        if timestamp in self.received_schedules.keys():
            del self.received_schedules[timestamp]

    async def apply_control_mechanisms(self, timestamp):
        """Creates a control signal with regard to control type strategy."""

        # go by each congestion and save steps with congestions
        steps_curtail_demand = []
        steps_curtail_generation = []
        for cong_dict in self.congestions:
            if cong_dict["curtail"] == "gen":
                steps_curtail_generation.append(cong_dict["step"])
            elif cong_dict["curtail"] == "dem":
                steps_curtail_demand.append(cong_dict["step"])
        # take unique values and sort them
        steps_curtail_demand = sorted(set(steps_curtail_demand))
        steps_curtail_generation = sorted(set(steps_curtail_generation))

        # create signal with regard to control strategy
        if self.control_type == "tariff":
            # adjust tariff
            for step in steps_curtail_demand:
                self.control_signal.tariff_adj[step] = self.control_conf["TARIFF_ADJ"]
            for step in steps_curtail_generation:
                self.control_signal.tariff_adj[step] = -self.control_conf["TARIFF_ADJ"]
            self.control_signal.tariff_adj_feedin = self.control_conf[
                "TARIFF_ADJ_FEEDIN"
            ]
        elif self.control_type == "limits":
            # adjust power limits (with check wether there already was a limit or not)
            for step in steps_curtail_demand:
                self.control_signal.p_max[step] = min(
                    self.control_signal.p_max[step]
                    + self.control_conf["P_MAX_STEP_kW"],
                    self.control_conf["P_MAX_INIT_kW"],
                )
            for step in steps_curtail_generation:
                self.control_signal.p_min[step] = max(
                    self.control_signal.p_min[step]
                    + self.control_conf["P_MIN_STEP_kW"],
                    self.control_conf["P_MIN_INIT_kW"],
                )
        elif self.control_type == "peak_price":
            # set peak price for both directions
            self.control_signal.peak_price_dem = self.control_conf["PEAK_PRICE_DEM"]
            self.control_signal.peak_price_gen = self.control_conf["PEAK_PRICE_GEN"]
        elif self.control_type == "conditional_power":
            # set power threshold and additional costs for power above
            self.control_signal.conditional_power_threshold = self.control_conf[
                "COND_POWER_THRESHOLD_kW"
            ]
            self.control_signal.conditional_power_add_costs = self.control_conf[
                "COND_POWER_ADD_COSTS"
            ]
        elif self.control_type == "none":
            # no control for participants is executed
            pass
        else:
            raise TypeError(
                f"No control type '{self.control_type}' implemented for Central Instance!"
                "Only 'tariff', 'limits', and 'peak_price' possible for CONTROL_TYPE!"
            )

        # send signals to each participant
        for participant in self.load_participant_coord.keys():
            content = self.control_signal

            # collect meta data of central instance
            acl_meta = {"sender_id": self.aid, "sender_addr": self.addr}

            # send message to each participant
            self.schedule_instant_acl_message(
                content,
                (participant.host, participant.port),
                participant.agent_id,
                acl_metadata=acl_meta,
            )

    def send_time_step_done_to_syncing_agent(self, sender, c_id):
        # send reply
        content = TimeStepReply(c_id)
        acl_meta = {"sender_id": self.aid, "sender_addr": self.addr}
        self.schedule_instant_acl_message(
            content,
            (sender.host, sender.port),
            sender.agent_id,
            acl_metadata=acl_meta,
        )

    def reset_control_signal(self, timestamp: int):
        """Initialize "zero" signals for control."""
        self.control_signal = ControlMechanismMessage(
            timestamp=timestamp,
            tariff_adj=np.zeros(self.steps_day),
            p_max=np.inf * np.ones(self.steps_day),
            p_min=-np.inf * np.ones(self.steps_day),
            peak_price_gen=0.0,
            peak_price_dem=0.0,
        )

    async def compute_time_step(self, timestamp, sender, c_id):
        # collect agents residual schedules
        if timestamp % ONE_DAY_IN_SECONDS == 0:
            self.time_step_done = False

            # always happens at least once
            await self.get_participant_schedules(timestamp)
            list_results_bus, list_results_line = await self.calculate_grid_schedule(
                timestamp
            )
            self.time_step_done = self.check_schedule_ok(
                list_results_bus, list_results_line
            )

            # clear sent signals from steps before
            self.reset_control_signal(timestamp=timestamp)

            step_loops = 1
            # possibly adjust maximum number of loops according to control type
            if self.control_type == "conditional_power":
                self.control_conf["MAX_NUM_LOOPS"] = 1  # max. one control signal sent
                logging.warning(
                    f"Max. # of loops for control type conditional_power = 1!"
                )
                if self.control_conf["COND_POWER_SEND_ALWAYS"]:
                    self.time_step_done = (
                        False  # signal to be sent no matter of congestion
                    )
            elif self.control_type == "peak_price":
                self.control_conf["MAX_NUM_LOOPS"] = 1  # max. one control signal sent
                logging.warning(f"Max. # of loops for control type peak_price = 1!")
                if self.control_conf["PEAK_PRICE_SEND_ALWAYS"]:
                    self.time_step_done = (
                        False  # signal to be sent no matter of congestion
                    )
            elif self.control_type == "none":
                self.control_conf["MAX_NUM_LOOPS"] = 0  # no control signal sent
                logging.warning(f"Max. # of loops for control type none = 0!")

            # gets instantly skipped if schedules are already ok
            # flag gets set by calculate_grid_schedule when the schedule
            # fulfilly all requirements
            while not self.time_step_done:
                # check if looping of sending control signals exceeded max.
                if step_loops > self.control_conf["MAX_NUM_LOOPS"]:
                    logging.warning(
                        f"Could not resolve all issues in time step: {timestamp}"
                    )
                    self.time_step_done = True
                    continue

                logging.info(
                    f"Central Instance - Loop number {step_loops} for step {timestamp}."
                )

                # TODO: maybe ensure mechanism is always a strong enough for compliance? (probably not possible)
                self.clear_local_schedules(timestamp)
                await self.apply_control_mechanisms(timestamp)
                await self.get_participant_schedules(timestamp)
                list_results_bus, list_results_line = (
                    await self.calculate_grid_schedule(timestamp)
                )
                self.time_step_done = self.check_schedule_ok(
                    list_results_bus, list_results_line
                )
                step_loops += 1

            # store list_results in result_timeseries JUST FOR LAST SCHEDULE CALCULATION
            for bus_id in self.result_timeseries_bus_vm_pu.keys():
                self.result_timeseries_bus_vm_pu[bus_id].append(
                    list_results_bus[bus_id]
                )
            for line_id in self.result_timeseries_line_load.keys():
                self.result_timeseries_line_load[line_id].append(
                    list_results_line[line_id]
                )

            self.send_time_step_done_to_syncing_agent(sender, c_id)

            logging.info(
                f"Central Instance calculated for timestamp {timestamp} --- {time_int_to_str(timestamp)}."
            )

    def init_grid(self, grid_name: str):
        # load grid from pandapower
        if grid_name == "kerber_dorfnetz":
            self.grid = ppnet.create_kerber_dorfnetz()
        elif grid_name == "kerber_landnetz":
            self.grid = ppnet.create_kerber_landnetz_kabel_1()
        elif any(string in grid_name for string in ["rural", "semiurb", "urban"]):
            self.grid = sb.get_simbench_net(sb_code_info=grid_name)
            if grid_name == "1-LV-rural1--0-sw" or grid_name == "1-LV-rural1--0-no_sw":
                self.grid.bus.index = self.grid.bus.index.where(
                    self.grid.bus.index != 42, 14
                )
            if grid_name == "1-LV-rural2--1-sw":
                self.grid.bus.loc[0, "vm_pu"] = 1.0
        else:
            raise ValueError(
                f"Grid {self.aid} could not be created - found no grid named {grid_name}"
            )

        # set all load (re)active power values to 0
        for idx_l in range(len(self.grid.load)):
            self.grid.load.at[idx_l, "p_mw"] = 0
            self.grid.load.at[idx_l, "q_mvar"] = 0  # reactive power value

        # store IDs of loadbuses
        self.load_bus_names = [x for x in self.grid.bus["name"] if x.startswith("load")]
        if self.load_bus_names == []:  # no loads - seems to be simbench grid
            self.load_bus_names = [
                b_name
                for idx, b_name in enumerate(self.grid.bus["name"])
                if b_name.startswith("LV") and idx in self.grid.load["bus"].values
            ]

    def set_inputs(self, data_for_buses):
        """Set active/reactive power values for the grid loads."""
        for name_bus, power_value in data_for_buses.items():
            # get the index of the bus
            idx_b = self.grid.bus.index[self.grid.bus["name"] == name_bus].to_list()[0]
            # get the index of the load
            idx_load_to_bus = self.grid.load["bus"] == idx_b
            load_list = self.grid.load.index[idx_load_to_bus].to_list()

            for i in range(len(load_list)):
                if i == 0:  # only for first load, not for all loads at this bus
                    # set active power (and remove reactive power value)
                    self.grid.load.at[load_list[i], "p_mw"] = (
                        power_value / 1e3
                    )  # active power kW->MW
                    self.grid.load.at[load_list[i], "q_mvar"] = (
                        0  # reactive power value
                    )
                else:
                    self.grid.load.at[load_list[i], "p_mw"] = 0
                    self.grid.load.at[load_list[i], "q_mvar"] = 0

    def calc_grid_powerflow(self):
        pp.runpp(
            self.grid,
            numba=False,
            calculate_voltage_angles=False,
        )

    def store_grid_results(self):
        self.grid_results_bus = {}
        self.grid_results_line = {}
        if not self.grid.res_bus.empty:  # powerflow converged
            for i_bus in range(len(self.grid.bus)):
                self.grid_results_bus[self.grid.bus.loc[i_bus, "name"]] = np.round(
                    self.grid.res_bus.loc[i_bus, "vm_pu"], 5
                )
            for i_line in range(len(self.grid.line)):
                self.grid_results_line[self.grid.line.loc[i_line, "name"]] = np.round(
                    self.grid.res_line.loc[i_line, "loading_percent"], 2
                )
            for i_trafo in range(len(self.grid.trafo)):
                self.grid_results_line[self.grid.trafo.loc[i_trafo, "name"]] = np.round(
                    self.grid.res_trafo.loc[i_trafo, "loading_percent"], 2
                )

    def run(self):
        # to proactively do things
        pass

    def get_address(self):
        return AgentAddress(self.addr[0], self.addr[1], self.aid)
