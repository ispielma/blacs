import gtk
from calibrations import *

class AO(object):
    def __init__(self, name, channel, widget, combobox, calib_class, calib_params, default_units, static_update_function, min, max, step, value = 0, current_units = None):
        self.adjustment = gtk.Adjustment(value,min,max,step,10*step,0)
        self.handler_id = self.adjustment.connect('value-changed',static_update_function)
        self.name = name
        self.channel = channel
        self.locked = False
        self.comboboxmodel = combobox.get_model()
        self.comboboxes = []
        self.comboboxhandlerids = []
        self.current_units = default_units
        self.base_unit = default_units
        self.limits = [min,max]
        
        
        # Initialise Calibrations
        if calib_class is not None:
            if calib_class not in globals() or not isinstance(calib_params,dict) or test.base_unit != default_units:
                # Throw an error:
                # Use default units
                self.calibration = None
                self.comboboxmodel.append([default_units])
            else:
                # initialise calibration class
                self.calibration = globals()[calib_class](calib_params)                
                self.comboboxmodel.append([self.calibration.base_unit])
                        
                for unit in self.calibration.human_units:
                    self.comboboxmodel.append([unit])
                    
                combobox.set_active(0)
        else:
            # use default units
            self.calibration = None
            self.comboboxmodel.append([default_units])
        
        self.add_widget(widget,combobox)
    
    def update(self,settings):
        if 'front_panel_settings' in settings:
            if self.channel in settings['front_panel_settings']:
                saved_data = settings['front_panel_settings'][self.channel]
                # Update the unit selection
                self.change_units(saved_data['current_units'])
                
                # Update the value
                self.set_value(saved_data['base_value'],program=False)

                # Update the step size
                self.set_step_size_in_base_units(saved_data['base_step_size'])
                
                # Update the Lock
                self.locked = saved_data['locked']
                self.update_lock()
    
    def add_widget(self,widget, combobox):
        widget.set_adjustment(self.adjustment)
        # Set the model to match the other comboboxes
        combobox.set_model(self.comboboxmodel)
        # set the active item to match the active item of one of the comboboxes
        if self.comboboxes:
            combobox.set_active(self.comboboxes[0].get_active())
        else:
            combobox.set_active(0)
        self.comboboxes.append(combobox)
        self.comboboxhandlerids.append(combobox.connect('changed',self.on_selection_changed))
        
        # Add signal to populate the right click context menu with our own things!
        widget.connect("populate-popup", self.populate_context_menu)
        widget.connect("button-release-event",self.on_button_release)
     
    def on_selection_changed(self,combobox):
        for box, id in zip(self.comboboxes,self.comboboxhandlerids):
            if box is not combobox:
                box.handler_block(id)
                box.set_selection(combobox.get_selection())
                box.handler_unblock(id)
                
        # Update the parameters of the Adjustment to match the new calibration!
        new_units = self.comboboxmodel.get(combobox.get_active_iter(),0)[0]
        
        # do derivative on step size after conversion to get a correct conversion
        step_lower = self.adjustment.get_value()
        step_upper = self.adjustment.get_value() + self.adjustment.get_step_increment()
        
        parameter_list = [self.adjustment.get_value(),self.adjustment.get_lower(),self.adjustment.get_upper(),step_lower,step_upper,
                          self.limits[0],self.limits[1]]
        
        # If we aren't alreay in base units, convert to base units
        if self.current_units != self.calibration.base_unit:
            # get the conversion function
            convert = getattr(self.calibration,self.current_units+"_to_base")
            for index,param in enumerate(parameter_list):
                #convert each to base units
                parameter_list[index] = convert(param)
        
        # Now convert to the new unit
        if new_units != self.calibration.base_unit:
            convert = getattr(self.calibration,new_units+"_from_base")
            for index,param in enumerate(parameter_list):
                #convert each to base units
                parameter_list[index] = convert(param)
        
        # Store the current units
        self.current_units = new_units
        
        # Check to see if the upper/lower bound has switched
        if parameter_list[1] > parameter_list[2]:
            parameter_list[1], parameter_list[2] = parameter_list[2], parameter_list[1]
        
        # Block the signal (nothing has actually changed in the value to program)
        self.adjustment.handler_block(self.handler_id)            
        # Update the Adjustment
        self.adjustment.configure(parameter_list[0],parameter_list[1],parameter_list[2],abs(parameter_list[3]-parameter_list[4]),abs(parameter_list[3]-parameter_list[4])*10,0)
        #Unblock the handler
        self.adjustment.handler_unblock(self.handler_id)
        
        # update saved limits
        if parameter_list[5] > parameter_list[6]:
            parameter_list[5], parameter_list[6] = parameter_list[6], parameter_list[5] 
        self.limits = [parameter_list[5], parameter_list[6]]
     
    def change_units(self,unit):
        # default to base units
        unit_index = 0
        
        if self.calibration:
            i = 1
            for unit_choice in self.calibration.human_units:
                if unit_choice == unit:
                    unit_index = i
                i += 1
            
        # Set one of the comboboxes to the correct unit (the rest will be updated automatically)
        self.comboboxes[0].set_active(unit_index)
    
    @property
    def value(self):
        value = self.adjustment.get_value()
        # If we aren't already in base units, convert to base units
        if self.current_units != self.base_unit: 
            convert = getattr(self.calibration,self.current_units+"_to_base")
            value = convert(value)
        return value
        
    def set_value(self, value, program=True):
        # conversion to float means a string can be passed in too:
        value = float(value)
        # If we aren't in base units, convert to the new units!
        if self.current_units != self.base_unit: 
            convert = getattr(self.calibration,self.current_units+"_from_base")
            value = convert(value)
            
        if not program:
            self.adjustment.handler_block(self.handler_id)
        if value != self.value:            
            self.adjustment.set_value(value)
        if not program:
            self.adjustment.handler_unblock(self.handler_id)
    
    def set_limits(self, menu_item):
        pass
        
    def change_step(self, menu_item):
        def handle_entry(widget,dialog):
            dialog.response(gtk.RESPONSE_ACCEPT)
        
        dialog = gtk.Dialog("My dialog",
                     None,
                     gtk.DIALOG_MODAL,
                     (gtk.STOCK_CANCEL, gtk.RESPONSE_REJECT,
                      gtk.STOCK_OK, gtk.RESPONSE_ACCEPT))
        
        label = gtk.Label("Set the step size for the up/down controls on the spinbutton in %s"%self.current_units)
        dialog.vbox.pack_start(label, expand = False, fill = False)
        label.show()
        entry = gtk.Entry()
        entry.connect("activate",handle_entry,dialog)
        dialog.get_content_area().pack_end(entry)
        entry.show()
        response = dialog.run()
        value_str = entry.get_text()
        dialog.destroy()
        
        if response == gtk.RESPONSE_ACCEPT:
            
            try:
                # Get the value from the entry
                value = float(value_str)
                
                # Check if the value is valid
                if value > (self.limits[1] - self.limits[0]):
                    raise Exception("The step size specified is greater than the difference between the current limits")
                
                self.adjustment.set_step_increment(value)
                self.adjustment.set_page_increment(value*10)
                
            except Exception, e:
                # Make a message dialog with an error in
                dialog = gtk.MessageDialog(None,
                     gtk.DIALOG_MODAL,
                     gtk.MESSAGE_ERROR,
                     gtk.BUTTONS_NONE,
                     "An error occurred while updating the step size:\n\n%s"%e.message)
                     
                dialog.run()
                dialog.destroy()
    
    def set_step_size_in_base_units(self,step_size):
        # convert to current units
        step_size_upper = self.adjustment.get_value()+step_size
        step_size_lower = self.adjustment.get_value()
        if self.current_units != self.base_unit: 
            convert = getattr(self.calibration,self.current_units+"_from_base")
            step_size_lower = convert(step_size_lower)
            step_size_upper = convert(step_size_upper)
        
        self.adjustment.set_step_increment(abs(step_size_lower-step_size_upper))
        self.adjustment.set_page_increment(abs(step_size_lower-step_size_upper)*10)
    
    def get_step_in_base_units(self):
        value = self.adjustment.get_step_increment() + self.adjustment.get_value()
        value2 = self.adjustment.get_value()
        
        if self.current_units != self.base_unit: 
            convert = getattr(self.calibration,self.current_units+"_to_base")
            value = convert(value)
            value2 = convert(value2)
        return abs(value - value2)
    
    def lock(self, menu_item):
        self.locked = not self.locked
        self.update_lock()
    
    def update_lock(self):    
        if self.locked:
            # Save the limits (this will be inneccessary once we implement software limits)
            self.limits = [self.adjustment.get_lower(),self.adjustment.get_upper()]
            
            # Set the limits equal to the value
            value = self.adjustment.get_value()
            self.adjustment.set_lower(value)
            self.adjustment.set_upper(value)
        else:
            # Restore the limits
            self.adjustment.set_lower(self.limits[0])
            self.adjustment.set_upper(self.limits[1])
        
    def populate_context_menu(self,widget,menu):
        # is it a right click?
        menu_item2 = gtk.MenuItem("Unlock Widget" if self.locked else "Lock Widget")
        menu_item2.connect("activate",self.lock)
        menu_item2.show()
        menu.append(menu_item2)
        menu_item3 = gtk.MenuItem("Change step size")
        menu_item3.connect("activate",self.change_step)
        menu_item3.show()
        menu.append(menu_item3)
        sep = gtk.SeparatorMenuItem()
        sep.show()
        menu.append(sep)
        # reorder children
        menu.reorder_child(menu_item2,0)
        menu.reorder_child(menu_item3,1)
        menu.reorder_child(sep,2)
        
    def on_button_release(self,widget,event):
        if event.button == 3:
            menu = gtk.Menu()
            self.populate_context_menu(widget,menu)
            menu.popup(None,None,None,event.button,event.time)
            
class DO(object):
    def __init__(self, name, channel, widget, static_update_function):
        self.action = gtk.ToggleAction('%s\n%s'%(channel,name), '%s\n%s'%(channel,name), "", 0)
        self.handler_id0 = self.action.connect('toggled',self.check_lock)
        self.handler_id = self.action.connect('toggled',static_update_function)
        self.handler_id2 = self.action.connect('toggled',self.update_style)
        self.name = name
        self.channel = channel
        self.widget_list = []
        self.add_widget(widget)
        self.locked = False
        self.current_state = self.action.get_active()
        self.update_style()
    
    def update(self,settings):
        if 'front_panel_settings' in settings:
            if self.channel in settings['front_panel_settings']:
                saved_data = settings['front_panel_settings'][self.channel]
                # Update the value
                self.set_state(saved_data['base_value'],program=False)

                # Update the Lock
                self.locked = saved_data['locked']
                self.update_style()
    
    def add_widget(self,widget):
        self.action.connect_proxy(widget)
        widget.connect('button-release-event',self.btn_release)
        self.widget_list.append(widget)
        
    @property   
    def state(self):
        return bool(self.action.get_active())
    
    def lock(self,menuitem):
        self.locked = not self.locked
        #self.action.set_sensitive(not self.locked)
        self.update_style()
            
    def set_state(self,state,program=True):
        # conversion to integer, then bool means we can safely pass in
        # either a string '1' or '0', True or False or 1 or 0
        state = bool(int(state))
        
        # We are programatically setting the state, so break the check lock function logic
        self.current_state = state
        
        if not program:
            self.action.handler_block(self.handler_id)
        if state != self.state:
            self.action.set_active(state)
        if not program:
            self.action.handler_unblock(self.handler_id)
   
    def btn_release(self,widget,event):
        if event.button == 3:
            menu = gtk.Menu()
            menu_item = gtk.MenuItem("Unlock Widget" if self.locked else "Lock Widget")
            menu_item.connect("activate",self.lock)
            menu_item.show()
            menu.append(menu_item)
            menu.popup(None,None,None,event.button,event.time)
    
    def check_lock(self,widget):
        if self.locked and self.current_state != self.action.get_active():
            #Don't update the self.current_state variable 
            self.action.stop_emission('toggled')    
            self.action.handler_block(self.handler_id)
            self.action.set_active(self.current_state)
            self.action.handler_unblock(self.handler_id)
            
        else:
            self.current_state = self.action.get_active()
    
    def update_style(self,widget=None):
        for widget in self.widget_list:
            if self.state:
                widget.set_name('pressed_%stoggle_widget'%('disabled_' if self.locked else ''))
            else:
                widget.set_name('normal_%stoggle_widget'%('disabled_' if self.locked else ''))
            
        
class DDS(object):
    def __init__(self, freq, amp, phase, gate):
        self.amp = amp
        self.freq = freq
        self.phase = phase
        self.gate = gate
        