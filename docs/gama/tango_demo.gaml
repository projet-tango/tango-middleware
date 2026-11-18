/**
* Name: Tango demo
* Description: Three physical robots driven by GAMA through the Tango middleware.
*
*   1. Placement: GAMA asks the middleware to bring each robot to its start position
*      ({"place": {...}} on /tango/requests). The middleware drives them there one after the other,
*      using the camera, and reports "arrived" in /tango/robots/state.
*   2. Run: every cycle GAMA reads where the robots really are (/tango/robots/state) and sends each
*      one a speed and a heading (/tango/requests). Here each robot wanders between random waypoints
*      and keeps away from the others and from the borders.
*
*   Coordinates are GAMA's: the world is 100 x 100, origin at the top left, y pointing down, headings
*   in degrees (0 = east, clockwise). The middleware maps this world onto the area delimited by the
*   boundary tags (logical.width / logical.height = 100 in its configuration).
*
*   Needs the ROS 2 plugin for GAMA (https://github.com/projet-tango/gama.plugin.ros) and the same
*   ROS_DOMAIN_ID as the middleware. If the middleware runs with another namespace than /tango,
*   change 'ns' below.
* Tags: ros, ros2, robots, tango
*/
model tango_demo

global {
	// ------------------------------------------------------------------ world (must match the middleware)
	float world_size <- 100.0;
	geometry shape <- envelope(square(world_size));

	// ------------------------------------------------------------------ demo parameters
	list<string> robot_names <- ["robot1", "robot2", "robot3"];
	// start positions (GAMA units) and headings (degrees): a row near the bottom, facing up
	list<point> start_positions <- [{25.0, 80.0}, {50.0, 80.0}, {75.0, 80.0}];
	float start_heading <- 270.0;
	float cruise_speed <- 8.0;      // GAMA units per second (100 units = the width of the area)
	float keep_away <- 15.0;        // distance kept from the other robots
	float border <- 10.0;           // waypoints stay this far from the edges

	// ------------------------------------------------------------------ ROS
	string ns <- "/tango";
	ros_node node <- ros_node("gama_tango_demo");
	ros_topic requests_topic <- ros_topic(ns + "/requests", "std_msgs/msg/String");
	ros_topic state_topic <- ros_topic(ns + "/robots/state", "std_msgs/msg/String");
	ros_publisher requests <- ros_publisher(node, requests_topic);
	ros_subscription states <- ros_subscription(node, state_topic, 1);

	// placing -> running
	string phase <- "placing";
	float step <- 0.1 #s;

	init {
		loop i from: 0 to: length(robot_names) - 1 {
			create tango_robot {
				name <- robot_names[i];
				home <- start_positions[i];
				location <- home;
			}
		}
		bool ready <- ros_wait_for_subscriber(requests, 3000);
		if !ready {
			write "Nobody listens on " + ns + "/requests: is the middleware running, with the same ROS_DOMAIN_ID?";
		}
		// ask the middleware to place every robot, one after the other
		map<string, unknown> targets <- [];
		ask tango_robot {
			targets[name] <- ["x"::home.x, "y"::home.y, "heading"::start_heading];
		}
		bool sent <- ros_publish(requests, ["data"::to_json(["place"::targets])]);
		write "Placement requested: " + targets;
	}

	// ------------------------------------------------------------------ perception: where the robots really are
	reflex read_state {
		map<string, unknown> msg <- ros_read_latest(states);
		if msg != nil {
			map<string, unknown> state <- map<string, unknown>(from_json(string(msg["data"])));
			map<string, unknown> robots_state <- map<string, unknown>(state["robots"]);
			ask tango_robot {
				if robots_state contains_key name {
					do update(map<string, unknown>(robots_state[name]));
				}
			}
		}
	}

	// ------------------------------------------------------------------ placement done?
	reflex wait_for_placement when: phase = "placing" {
		if tango_robot all_match (each.goal_status = "arrived") {
			phase <- "running";
			write "All robots in place at cycle " + cycle + ": starting the demo";
		} else if tango_robot one_matches (each.goal_status = "failed") {
			write "A robot could not reach its start position: " + ((tango_robot where (each.goal_status = "failed")) collect each.name);
			phase <- "running";
		}
	}

	// ------------------------------------------------------------------ action: one request per robot, every cycle
	reflex drive when: phase = "running" {
		list<map<string, unknown>> orders <- [];
		ask tango_robot {
			orders <+ ["robot"::name, "speed"::wanted_speed, "heading"::wanted_heading];
		}
		bool sent <- ros_publish(requests, ["data"::to_json(["requests"::orders])]);
	}
}

species tango_robot {
	point home;
	bool visible <- false;
	float heading <- 0.0;           // measured, degrees, GAMA convention
	string goal_status <- "";       // placement: queued, moving, turning, waiting, arrived, failed
	point waypoint;
	float wanted_speed <- 0.0;
	float wanted_heading <- 0.0;

	action update(map<string, unknown> s) {
		visible <- bool(s["visible"]);
		map<string, unknown> lg <- map<string, unknown>(s["logical"]);
		if bool(s["ever_seen"]) {
			location <- {float(lg["x"]), float(lg["y"])};
			heading <- float(lg["heading"]);
		}
		if s["goal"] != nil {
			goal_status <- string(map<string, unknown>(s["goal"])["status"]);
		}
	}

	// wander between random waypoints, keep away from the other robots
	reflex decide when: phase = "running" {
		if waypoint = nil or location distance_to waypoint < 5.0 {
			waypoint <- {rnd(border, world_size - border), rnd(border, world_size - border)};
		}
		point push <- {0.0, 0.0};
		ask tango_robot - self {
			float d <- myself.location distance_to location;
			if d < keep_away and d > 0 {
				push <- push + (myself.location - location) * ((keep_away - d) / d);
			}
		}
		point direction <- (waypoint - location) + push * 2.0;
		wanted_heading <- atan2(direction.y, direction.x);   // degrees, clockwise on screen: GAMA = middleware
		wanted_speed <- visible ? cruise_speed : 0.0;
	}

	aspect default {
		rgb c <- visible ? (goal_status in ["moving", "turning", "waiting", "queued"] ? #orange : #steelblue) : #lightgray;
		draw circle(4.0) color: c border: #black;
		draw line([location, location + {cos(heading) * 6.0, sin(heading) * 6.0}]) color: #black width: 2;
		draw name at: location + {0.0, -6.0} color: #black font: font("SansSerif", 12, #bold);
		if waypoint != nil and phase = "running" {
			draw circle(1.2) at: waypoint color: c;
			draw line([location, waypoint]) color: c;
		}
		if phase = "placing" {
			draw square(6.0) at: home color: #transparent border: c;
		}
	}
}

experiment demo type: gui {
	// GAMA must run in real time: the robots move in the real world
	float minimum_cycle_duration <- 0.1;
	parameter "Cruise speed (units/s)" var: cruise_speed min: 0.0 max: 30.0;
	parameter "Distance kept between robots" var: keep_away min: 0.0 max: 40.0;

	output {
		display "Arena" type: 2d background: #white {
			graphics "field" {
				draw shape color: #whitesmoke border: #gray;
			}
			species tango_robot aspect: default;
		}
		monitor "Phase" value: phase;
		monitor "State messages received" value: ros_received(states);
	}
}
